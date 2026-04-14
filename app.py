"""
Document Management System with OCR, Approval Workflow, and Analytics
"""

import os
import re
import io
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum
from contextlib import asynccontextmanager

# FastAPI and dependencies
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Enum as SQLEnum, ForeignKey, Boolean, Text
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from jose import JWTError, jwt
from passlib.context import CryptContext

# Document processing
import pytesseract
from PIL import Image
import pdf2image
import cv2
import numpy as np

# Data processing and reporting
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

# Fuzzy matching
from rapidfuzz import process, fuzz

# Configuration
from dotenv import load_dotenv
import uvicorn

load_dotenv()

# ==================== Configuration ====================

class Config:
    """Application configuration"""
    SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this-in-production")
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 30))
    
    # Database Configuration
    PG_HOST = os.getenv("PG_HOST", "localhost")
    PG_PORT = os.getenv("PG_PORT", "5432")
    PG_USER = os.getenv("PG_USER", "postgres")
    PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres")
    PG_DB = os.getenv("PG_DB", "doc_management")
    
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB = os.getenv("MYSQL_DB", "doc_management")
    
    DATABASE_TYPE = os.getenv("DATABASE_TYPE", "postgresql").lower()
    RENDER_DATABASE_URL = os.getenv("DATABASE_URL")
    SQLITE_URL = "sqlite:///./doc_management.db"
    
    # File settings
    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 10 * 1024 * 1024))
    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
    
    # Known vendors for smart extraction
    KNOWN_VENDORS = [
        "Amazon", "Microsoft", "Google", "MTN", "Vodacom",
        "Telkom", "Spar", "Pick n Pay", "Shoprite"
    ]
    
    @classmethod
    def get_database_url(cls) -> str:
        """Get database URL based on configuration"""
        if cls.RENDER_DATABASE_URL:
            database_url = cls.RENDER_DATABASE_URL
            if database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql://", 1)
            return database_url
        
        if cls.DATABASE_TYPE == "postgresql":
            return f"postgresql://{cls.PG_USER}:{cls.PG_PASSWORD}@{cls.PG_HOST}:{cls.PG_PORT}/{cls.PG_DB}"
        elif cls.DATABASE_TYPE == "mysql":
            return f"mysql+pymysql://{cls.MYSQL_USER}:{cls.MYSQL_PASSWORD}@{cls.MYSQL_HOST}:{cls.MYSQL_PORT}/{cls.MYSQL_DB}"
        else:
            return cls.SQLITE_URL
    
    @classmethod
    def validate(cls) -> bool:
        """Validate critical configuration"""
        if cls.SECRET_KEY == "your-secret-key-change-this-in-production":
            print("⚠️ WARNING: Using default SECRET_KEY. Set a secure key in .env file!")
        return True


# ==================== Database Models ====================

Base = declarative_base()

class UserRole(str, Enum):
    ADMIN = "ADMIN"
    APPROVER = "APPROVER"
    MANAGER = "MANAGER"
    VIEWER = "VIEWER"

class ApprovalStatus(str, Enum):
    PENDING_LEVEL1 = "PENDING_LEVEL1"
    PENDING_LEVEL2 = "PENDING_LEVEL2"
    PENDING_LEVEL3 = "PENDING_LEVEL3"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    email = Column(String(100), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(SQLEnum(UserRole), nullable=False)
    full_name = Column(String(100))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class Document(Base):
    __tablename__ = "documents"
    
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_hash = Column(String(64), unique=True)
    document_type = Column(String(20), nullable=False)
    vendor_name = Column(String(200))
    invoice_number = Column(String(100), index=True)
    invoice_date = Column(DateTime)
    amount = Column(Float)
    vat_amount = Column(Float)
    upload_date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    status = Column(SQLEnum(ApprovalStatus), default=ApprovalStatus.PENDING_LEVEL1)
    is_duplicate = Column(Boolean, default=False)
    duplicate_reason = Column(Text)
    
    uploader = relationship("User")

class Approval(Base):
    __tablename__ = "approvals"
    
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    approver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    approval_level = Column(Integer)
    decision = Column(String(20))
    comments = Column(Text)
    approved_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String(100))
    details = Column(Text)
    ip_address = Column(String(45))
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ==================== Database Setup ====================

def create_database_engine():
    """Create database engine with fallback to SQLite"""
    try:
        database_url = Config.get_database_url()
        
        if "postgresql" in database_url:
            engine = create_engine(
                database_url,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
                pool_recycle=3600,
                echo=False
            )
        else:
            engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=3600)
        
        print(f"✅ Database connected: {Config.DATABASE_TYPE}")
        return engine
        
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        print("⚠️ Falling back to SQLite...")
        return create_engine(Config.SQLITE_URL, connect_args={"check_same_thread": False})

engine = create_database_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    """Dependency for database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==================== Pydantic Schemas ====================

class UserLogin(BaseModel):
    username: str
    password: str

class UserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    full_name: str
    role: UserRole

class Token(BaseModel):
    access_token: str
    token_type: str

class ApprovalAction(BaseModel):
    document_id: int
    decision: str
    comments: Optional[str] = None

class ReportFilter(BaseModel):
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    vendor_name: Optional[str] = None
    status: Optional[str] = None
    min_amount: Optional[float] = None
    max_amount: Optional[float] = None


# ==================== Authentication ====================

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, Config.SECRET_KEY, algorithm=Config.ALGORITHM)

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """Get current authenticated user"""
    token = None
    if credentials:
        token = credentials.credentials
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(token, Config.SECRET_KEY, algorithms=[Config.ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User inactive or not found")
    return user

def role_required(required_roles: List[UserRole]):
    """Dependency for role-based access control"""
    async def checker(current_user: User = Depends(get_current_user)):
        if current_user.role not in required_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return checker


# ==================== File Utilities ====================

def validate_file(file_content: bytes, filename: str) -> None:
    """Validate file size and extension"""
    if len(file_content) > Config.MAX_FILE_SIZE:
        raise HTTPException(400, f"Max size {Config.MAX_FILE_SIZE // 1024 // 1024}MB")
    
    ext = os.path.splitext(filename)[1].lower()
    if ext not in Config.ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Allowed extensions: {Config.ALLOWED_EXTENSIONS}")
    
    # Validate magic bytes
    magic_map = {b'%PDF': '.pdf', b'\xff\xd8': '.jpg', b'\x89PNG': '.png'}
    for magic, expected_ext in magic_map.items():
        if file_content.startswith(magic) and expected_ext != ext:
            raise HTTPException(400, "File extension mismatch")

def generate_secure_filename(original: str) -> str:
    """Generate secure unique filename"""
    ext = os.path.splitext(original)[1].lower()
    return f"{uuid.uuid4().hex}{ext}"


# ==================== AI Document Extractor ====================

class AIExtractor:
    """Extract document data using OCR and pattern matching"""
    
    @staticmethod
    async def extract_from_image(image_path: str) -> Dict[str, Any]:
        try:
            processed = AIExtractor._preprocess_image(image_path)
            text = pytesseract.image_to_string(processed, config="--psm 6")
            return AIExtractor._run_pipeline(text)
        except Exception as e:
            print(f"OCR error: {e}")
            return AIExtractor._empty()
    
    @staticmethod
    async def extract_from_pdf(pdf_path: str) -> Dict[str, Any]:
        try:
            images = pdf2image.convert_from_path(pdf_path, dpi=300)
            full_text = ""
            for img in images[:3]:
                img_np = np.array(img)
                processed = AIExtractor._preprocess_array(img_np)
                full_text += pytesseract.image_to_string(processed, config="--psm 6")
            return AIExtractor._run_pipeline(full_text)
        except Exception as e:
            print(f"PDF error: {e}")
            return AIExtractor._empty()
    
    @staticmethod
    def _run_pipeline(text: str) -> Dict[str, Any]:
        clean = AIExtractor._clean_text(text)
        regex_data, regex_conf = AIExtractor._regex_extract(clean)
        smart_data, smart_conf = AIExtractor._smart_extract(clean)
        final = AIExtractor._merge(regex_data, smart_data)
        final = AIExtractor._validate(final)
        
        return {
            **final,
            "confidence": {"regex": regex_conf, "smart": smart_conf},
            "raw_text_preview": clean[:500]
        }
    
    @staticmethod
    def _preprocess_image(path: str) -> np.ndarray:
        img = cv2.imread(path)
        return AIExtractor._preprocess_array(img)
    
    @staticmethod
    def _preprocess_array(img: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.fastNlMeansDenoising(gray, None, 30, 7, 21)
        return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
    
    @staticmethod
    def _clean_text(text: str) -> str:
        text = text.replace("\n", " ")
        text = re.sub(r"\s+", " ", text)
        text = text.replace("O", "0").replace("I", "1")
        return text.strip()
    
    @staticmethod
    def _regex_extract(text: str) -> Tuple[Dict, float]:
        data = {}
        patterns = {
            "invoice_number": r"(?:Invoice|INV)[\s#:]*([A-Z0-9-]+)",
            "amount": r"(?:Total|Amount Due)[\s:]*\$?([\d,]+\.\d{2})",
            "vat_amount": r"(?:VAT|Tax)[\s:]*\$?([\d,]+\.\d{2})",
            "invoice_date": r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
        }
        
        matches = 0
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                val = match.group(1)
                data[key] = AIExtractor._parse_value(key, val)
                matches += 1
        
        confidence = matches / len(patterns) if patterns else 0
        return data, confidence
    
    @staticmethod
    def _smart_extract(text: str) -> Tuple[Dict, float]:
        data = {}
        matches = 0
        
        # Vendor detection
        possible_vendor = " ".join(text.split()[:5])
        match = process.extractOne(possible_vendor, Config.KNOWN_VENDORS, scorer=fuzz.partial_ratio)
        if match and match[1] > 70:
            data["vendor_name"] = match[0]
            matches += 1
        
        # Amount fallback
        amounts = re.findall(r"\d+\.\d{2}", text)
        if amounts:
            data["amount"] = float(max(amounts))
            matches += 1
        
        confidence = matches / 2 if matches > 0 else 0
        return data, confidence
    
    @staticmethod
    def _parse_value(field: str, val: str):
        if field in ["amount", "vat_amount"]:
            try:
                return float(val.replace(",", ""))
            except ValueError:
                return None
        
        if field == "invoice_date":
            for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(val, fmt)
                except ValueError:
                    continue
            return None
        
        return val.strip()
    
    @staticmethod
    def _merge(regex_data: Dict, smart_data: Dict) -> Dict:
        keys = ["vendor_name", "invoice_number", "invoice_date", "amount", "vat_amount"]
        return {key: regex_data.get(key) or smart_data.get(key) for key in keys}
    
    @staticmethod
    def _validate(data: Dict) -> Dict:
        if data.get("amount") and data["amount"] < 0:
            data["amount"] = None
        if data.get("invoice_date") and data["invoice_date"] > datetime.now():
            data["invoice_date"] = None
        return data
    
    @staticmethod
    def _empty() -> Dict:
        return {
            "vendor_name": None, "invoice_number": None, "invoice_date": None,
            "amount": None, "vat_amount": None, "confidence": {}
        }


# ==================== Duplicate Detection ====================

class DuplicateDetector:
    """Detect duplicate documents using multiple similarity metrics"""
    
    @staticmethod
    def normalize_invoice(inv: str) -> str:
        if not inv:
            return ""
        return re.sub(r'[^A-Z0-9]', '', inv.upper())
    
    @staticmethod
    def normalize_vendor(name: str) -> str:
        if not name:
            return ""
        name = name.lower()
        name = re.sub(r'\b(ltd|pty|inc|corp|company)\b', '', name)
        return re.sub(r'[^a-z0-9]', '', name)
    
    @staticmethod
    def similarity_score(a: str, b: str) -> float:
        if not a or not b:
            return 0
        return fuzz.ratio(a, b) / 100
    
    @staticmethod
    def check_duplicate(
        db: Session,
        invoice_number: Optional[str],
        vendor_name: Optional[str],
        amount: Optional[float],
        file_content: bytes,
        document_type: str
    ) -> Tuple[bool, Optional[str], float]:
        """Check if document is a duplicate"""
        file_hash = hashlib.sha256(file_content).hexdigest()
        
        # Exact file match
        existing = db.query(Document).filter(Document.file_hash == file_hash).first()
        if existing:
            return True, f"Exact duplicate file (Doc #{existing.id})", 1.0
        
        inv_norm = DuplicateDetector.normalize_invoice(invoice_number)
        vendor_norm = DuplicateDetector.normalize_vendor(vendor_name)
        candidates = db.query(Document).all()
        
        best_score = 0
        best_match = None
        
        for doc in candidates:
            score = 0
            doc_inv = DuplicateDetector.normalize_invoice(doc.invoice_number)
            doc_vendor = DuplicateDetector.normalize_vendor(doc.vendor_name)
            
            inv_sim = DuplicateDetector.similarity_score(inv_norm, doc_inv)
            if inv_sim > 0.9:
                score += 0.5
            elif inv_sim > 0.7:
                score += 0.3
            
            vendor_sim = DuplicateDetector.similarity_score(vendor_norm, doc_vendor)
            if vendor_sim > 0.85:
                score += 0.3
            
            if amount and doc.amount:
                diff = abs(amount - doc.amount)
                if diff < 1:
                    score += 0.3
                elif diff / amount < 0.02:
                    score += 0.2
            
            if doc.invoice_date:
                days_diff = abs((doc.invoice_date - datetime.now(timezone.utc)).days)
                if days_diff < 30:
                    score += 0.1
            
            if score > best_score:
                best_score = score
                best_match = doc
        
        if best_score >= 0.7:
            return True, f"High confidence duplicate (Doc #{best_match.id})", best_score
        elif best_score >= 0.5:
            return True, f"Possible duplicate (Doc #{best_match.id})", best_score
        
        return False, None, best_score


# ==================== Database Initialization ====================

def init_database():
    """Initialize database tables and default users"""
    Base.metadata.create_all(bind=engine)
    os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
    
    db = SessionLocal()
    try:
        default_users = [
            ("admin", "admin@system.com", "Admin@123", UserRole.ADMIN, "System Administrator"),
            ("approver", "approver@system.com", "Approver@123", UserRole.APPROVER, "Level1 Approver"),
            ("manager", "manager@system.com", "Manager@123", UserRole.MANAGER, "Level2 Manager"),
            ("viewer", "viewer@system.com", "Viewer@123", UserRole.VIEWER, "Report Viewer")
        ]
        
        for username, email, pwd, role, fullname in default_users:
            if not db.query(User).filter(User.username == username).first():
                user = User(
                    username=username, email=email,
                    hashed_password=get_password_hash(pwd), role=role, full_name=fullname
                )
                db.add(user)
        db.commit()
        print("✅ Default users created")
    except Exception as e:
        print(f"⚠️ Database initialization error: {e}")
        db.rollback()
    finally:
        db.close()


# ==================== Lifespan Manager ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting Document Management System...")
    Config.validate()
    init_database()
    
    print("=" * 60)
    print("📄 Document Management System Ready!")
    print("=" * 60)
    print("Default Login Credentials:")
    print("  👑 Admin:    admin / Admin@123")
    print("  ✅ Approver: approver / Approver@123")
    print("  📊 Manager:  manager / Manager@123")
    print("  👁️ Viewer:   viewer / Viewer@123")
    print("=" * 60)
    
    yield
    print("🛑 Shutting down...")


# ==================== FastAPI App ====================

app = FastAPI(
    title="DocManager",
    version="3.0",
    description="Document Management System with OCR, Approval Workflow, and Analytics",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== Helper Functions ====================

def log_audit(db: Session, user_id: int, action: str, details: str, ip_address: str):
    """Log audit entry"""
    audit = AuditLog(user_id=user_id, action=action, details=details, ip_address=ip_address)
    db.add(audit)
    db.commit()


# ==================== API Routes ====================

@app.get("/")
async def root():
    return RedirectResponse(url="/login.html")

@app.post("/api/auth/login", response_model=Token)
async def login(
    request: Request,
    response: Response,
    user_login: UserLogin,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == user_login.username).first()
    if not user or not verify_password(user_login.password, user.hashed_password):
        raise HTTPException(401, "Invalid username or password")
    if not user.is_active:
        raise HTTPException(401, "Account disabled")
    
    token = create_access_token(
        data={"sub": user.username, "role": user.role.value},
        expires_delta=timedelta(minutes=Config.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    response.set_cookie(
        key="access_token", value=token, httponly=True,
        secure=False, samesite="lax", max_age=Config.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    
    log_audit(db, user.id, "LOGIN", "User logged in", 
              request.client.host if hasattr(request, 'client') else "unknown")
    
    return {"access_token": token, "token_type": "bearer"}

@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Logged out"}

@app.get("/api/auth/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id, "username": current_user.username,
        "email": current_user.email, "full_name": current_user.full_name,
        "role": current_user.role.value
    }

@app.post("/api/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    document_type: str = Form(...),
    current_user: User = Depends(role_required([UserRole.ADMIN, UserRole.APPROVER, UserRole.MANAGER])),
    db: Session = Depends(get_db)
):
    if document_type not in ["invoice", "credit_note"]:
        raise HTTPException(400, "Document type must be 'invoice' or 'credit_note'")
    
    content = await file.read()
    validate_file(content, file.filename)
    
    safe_name = generate_secure_filename(file.filename)
    file_path = os.path.join(Config.UPLOAD_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(content)
    
    file_hash = hashlib.sha256(content).hexdigest()
    ext = os.path.splitext(file.filename)[1].lower()
    
    if ext == ".pdf":
        extracted = await AIExtractor.extract_from_pdf(file_path)
    else:
        extracted = await AIExtractor.extract_from_image(file_path)
    
    is_dup, dup_reason, _ = DuplicateDetector.check_duplicate(
        db, extracted.get("invoice_number"), extracted.get("vendor_name"),
        extracted.get("amount"), content, document_type
    )
    
    doc = Document(
        filename=file.filename, file_path=file_path, file_hash=file_hash,
        document_type=document_type, vendor_name=extracted.get("vendor_name"),
        invoice_number=extracted.get("invoice_number"), invoice_date=extracted.get("invoice_date"),
        amount=extracted.get("amount"), vat_amount=extracted.get("vat_amount"),
        uploaded_by=current_user.id, status=ApprovalStatus.PENDING_LEVEL1,
        is_duplicate=is_dup, duplicate_reason=dup_reason
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    
    log_audit(db, current_user.id, "UPLOAD", f"Uploaded {file.filename} (ID: {doc.id})",
              request.client.host if hasattr(request, 'client') else "unknown")
    
    return {
        "message": "Document uploaded", "document_id": doc.id,
        "extracted_data": extracted, "is_duplicate": is_dup,
        "duplicate_reason": dup_reason, "status": doc.status.value
    }

@app.get("/api/documents")
async def list_documents(
    skip: int = 0, limit: int = 100, status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    query = db.query(Document)
    if current_user.role == UserRole.VIEWER:
        query = query.filter(Document.status == ApprovalStatus.APPROVED)
    if status_filter:
        query = query.filter(Document.status == status_filter)
    
    docs = query.order_by(Document.upload_date.desc()).offset(skip).limit(limit).all()
    
    return [{
        "id": d.id, "filename": d.filename, "document_type": d.document_type,
        "vendor_name": d.vendor_name, "invoice_number": d.invoice_number,
        "amount": d.amount, "status": d.status.value, "upload_date": d.upload_date,
        "is_duplicate": d.is_duplicate
    } for d in docs]

@app.get("/api/documents/{document_id}")
async def get_document(
    document_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    if current_user.role == UserRole.VIEWER and doc.status != ApprovalStatus.APPROVED:
        raise HTTPException(403, "Access denied - document not approved")
    
    approvals = db.query(Approval).filter(Approval.document_id == document_id).all()
    
    return {
        "document": {
            "id": doc.id, "filename": doc.filename, "document_type": doc.document_type,
            "vendor_name": doc.vendor_name, "invoice_number": doc.invoice_number,
            "invoice_date": doc.invoice_date, "amount": doc.amount, "vat_amount": doc.vat_amount,
            "status": doc.status.value, "upload_date": doc.upload_date,
            "is_duplicate": doc.is_duplicate, "duplicate_reason": doc.duplicate_reason
        },
        "approval_history": [{
            "level": a.approval_level, "decision": a.decision,
            "comments": a.comments, "approved_at": a.approved_at
        } for a in approvals]
    }

@app.post("/api/approval/process")
async def process_approval(
    action: ApprovalAction, request: Request,
    current_user: User = Depends(role_required([UserRole.ADMIN, UserRole.APPROVER, UserRole.MANAGER])),
    db: Session = Depends(get_db)
):
    doc = db.query(Document).filter(Document.id == action.document_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    if doc.status in [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]:
        raise HTTPException(400, f"Document already {doc.status.value}")
    
    # Determine allowed approval level
    level_map = {
        (UserRole.APPROVER, ApprovalStatus.PENDING_LEVEL1): 1,
        (UserRole.MANAGER, ApprovalStatus.PENDING_LEVEL2): 2,
        (UserRole.ADMIN, ApprovalStatus.PENDING_LEVEL3): 3
    }
    
    allowed_level = level_map.get((current_user.role, doc.status))
    if not allowed_level:
        raise HTTPException(403, "Not authorized for this approval stage")
    if action.decision not in ["approved", "rejected"]:
        raise HTTPException(400, "Decision must be 'approved' or 'rejected'")
    
    approval = Approval(
        document_id=doc.id, approver_id=current_user.id, approval_level=allowed_level,
        decision=action.decision, comments=action.comments
    )
    db.add(approval)
    
    if action.decision == "rejected":
        doc.status = ApprovalStatus.REJECTED
        message = f"Document #{doc.id} rejected at level {allowed_level}"
    else:
        if allowed_level == 1:
            doc.status = ApprovalStatus.PENDING_LEVEL2
            message = f"Document #{doc.id} approved at level 1, moved to level 2"
        elif allowed_level == 2:
            doc.status = ApprovalStatus.PENDING_LEVEL3
            message = f"Document #{doc.id} approved at level 2, moved to level 3"
        else:
            doc.status = ApprovalStatus.APPROVED
            message = f"Document #{doc.id} fully approved"
    
    db.commit()
    log_audit(db, current_user.id, "APPROVAL", f"Document {doc.id} {action.decision} at level {allowed_level}",
              request.client.host if hasattr(request, 'client') else "unknown")
    
    return {"message": message, "document_id": doc.id, "status": doc.status.value, "approval_level": allowed_level}

@app.get("/api/approval/pending")
async def get_pending_approvals(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    role_status_map = {
        UserRole.APPROVER: ApprovalStatus.PENDING_LEVEL1,
        UserRole.MANAGER: ApprovalStatus.PENDING_LEVEL2,
        UserRole.ADMIN: ApprovalStatus.PENDING_LEVEL3
    }
    
    status = role_status_map.get(current_user.role)
    if not status:
        return []
    
    docs = db.query(Document).filter(Document.status == status).all()
    return [{
        "id": d.id, "filename": d.filename, "vendor_name": d.vendor_name,
        "amount": d.amount, "status": d.status.value, "upload_date": d.upload_date
    } for d in docs]

# ==================== Report Routes (condensed) ====================

@app.post("/api/reports/spend-summary")
async def spend_summary(
    filters: ReportFilter, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    query = db.query(Document)
    if current_user.role == UserRole.VIEWER:
        query = query.filter(Document.status == ApprovalStatus.APPROVED)
    
    if filters.start_date:
        query = query.filter(Document.invoice_date >= filters.start_date)
    if filters.end_date:
        query = query.filter(Document.invoice_date <= filters.end_date)
    if filters.vendor_name:
        query = query.filter(Document.vendor_name.contains(filters.vendor_name))
    if filters.status:
        query = query.filter(Document.status == filters.status)
    if filters.min_amount is not None:
        query = query.filter(Document.amount >= filters.min_amount)
    if filters.max_amount is not None:
        query = query.filter(Document.amount <= filters.max_amount)
    
    docs = query.all()
    if not docs:
        return {"message": "No documents found matching criteria"}
    
    total_amount = sum(d.amount or 0 for d in docs)
    total_vat = sum(d.vat_amount or 0 for d in docs)
    
    vendor_breakdown = {}
    monthly_trend = {}
    for d in docs:
        if d.vendor_name:
            vendor_breakdown[d.vendor_name] = vendor_breakdown.get(d.vendor_name, 0) + (d.amount or 0)
        if d.invoice_date:
            month_key = d.invoice_date.strftime("%Y-%m")
            monthly_trend[month_key] = monthly_trend.get(month_key, 0) + (d.amount or 0)
    
    return {
        "summary": {
            "total_amount": total_amount, "total_vat": total_vat,
            "total_without_vat": total_amount - total_vat,
            "document_count": len(docs), "unique_vendors": len(vendor_breakdown)
        },
        "vendor_breakdown": vendor_breakdown, "monthly_trend": monthly_trend,
        "documents": [{"id": d.id, "vendor": d.vendor_name, "invoice_number": d.invoice_number,
                      "date": d.invoice_date, "amount": d.amount, "vat": d.vat_amount} for d in docs[:50]]
    }

@app.get("/api/reports/export/excel")
async def export_excel(
    start_date: Optional[datetime] = Query(None), end_date: Optional[datetime] = Query(None),
    vendor_name: Optional[str] = Query(None), status: Optional[str] = Query(None),
    min_amount: Optional[float] = Query(None), max_amount: Optional[float] = Query(None),
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    query = db.query(Document)
    if current_user.role == UserRole.VIEWER:
        query = query.filter(Document.status == ApprovalStatus.APPROVED)
    
    if start_date:
        query = query.filter(Document.invoice_date >= start_date)
    if end_date:
        query = query.filter(Document.invoice_date <= end_date)
    if vendor_name:
        query = query.filter(Document.vendor_name.contains(vendor_name))
    if status:
        query = query.filter(Document.status == status)
    if min_amount is not None:
        query = query.filter(Document.amount >= min_amount)
    if max_amount is not None:
        query = query.filter(Document.amount <= max_amount)
    
    docs = query.all()
    data = [{
        "Document ID": d.id, "Filename": d.filename, "Type": d.document_type,
        "Vendor": d.vendor_name or "", "Invoice Number": d.invoice_number or "",
        "Date": d.invoice_date, "Amount": d.amount or 0, "VAT Amount": d.vat_amount or 0,
        "Status": d.status.value, "Upload Date": d.upload_date
    } for d in docs]
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Documents', index=False)
        if data:
            pd.DataFrame([
                ["Total Amount", df["Amount"].sum()], ["Total VAT", df["VAT Amount"].sum()],
                ["Number of Documents", len(df)], ["Unique Vendors", df["Vendor"].nunique()],
                ["Average Amount", df["Amount"].mean()]
            ], columns=["Metric", "Value"]).to_excel(writer, sheet_name='Summary', index=False)
    
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f"attachment; filename=report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"})

# ==================== Analytics Routes ====================

@app.get("/api/analytics/insights")
async def get_ai_insights(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=365)
    
    query = db.query(Document).filter(Document.invoice_date >= start_date)
    if current_user.role == UserRole.VIEWER:
        query = query.filter(Document.status == ApprovalStatus.APPROVED)
    
    docs = query.all()
    if len(docs) < 5:
        return {"message": "Insufficient data for analysis (need at least 5 documents)"}
    
    amounts = [d.amount for d in docs if d.amount]
    vendor_spending = {}
    monthly_spending = {}
    
    for d in docs:
        if d.vendor_name and d.amount:
            vendor_spending[d.vendor_name] = vendor_spending.get(d.vendor_name, 0) + d.amount
        if d.invoice_date and d.amount:
            monthly_spending[d.invoice_date.strftime("%Y-%m")] = monthly_spending.get(d.invoice_date.strftime("%Y-%m"), 0) + d.amount
    
    # Detect anomalies
    anomalies = []
    if len(amounts) > 1:
        mean_amt = np.mean(amounts)
        std_amt = np.std(amounts)
        for d in docs:
            if d.amount and d.amount > mean_amt + 2 * std_amt:
                anomalies.append({
                    "document_id": d.id, "vendor": d.vendor_name, "amount": d.amount,
                    "date": d.invoice_date, "reason": f"Amount is {((d.amount - mean_amt) / std_amt):.1f} standard deviations above mean"
                })
    
    # Generate insights
    insights = []
    monthly_values = list(monthly_spending.values())
    if len(monthly_values) >= 2:
        change = ((monthly_values[-1] - monthly_values[-2]) / monthly_values[-2] * 100) if monthly_values[-2] > 0 else 0
        insights.append(f"{'📈' if change > 0 else '📉'} Spending {'increased' if change > 0 else 'decreased'} by {abs(change):.1f}% compared to last month")
    
    top_vendors = sorted(vendor_spending.items(), key=lambda x: x[1], reverse=True)[:3]
    if top_vendors:
        vendors_text = ", ".join([f"{v} (${a:,.0f})" for v, a in top_vendors])
        insights.append(f"🏢 Top 3 vendors: {vendors_text}")
    
    avg_amount = np.mean(amounts) if amounts else 0
    insights.append(f"💰 Average transaction amount: ${avg_amount:,.2f}")
    
    if monthly_spending:
        highest_month = max(monthly_spending, key=monthly_spending.get)
        insights.append(f"📅 Highest spending month: {highest_month} (${monthly_spending[highest_month]:,.2f})")
    
    return {
        "insights": insights, "anomalies": anomalies[:10],
        "statistics": {
            "total_spending": sum(amounts), "average_transaction": np.mean(amounts) if amounts else 0,
            "median_transaction": np.median(amounts) if amounts else 0, "total_transactions": len(docs),
            "unique_vendors": len(vendor_spending), "total_vat": sum(d.vat_amount or 0 for d in docs)
        },
        "trends": {"monthly_spending": monthly_spending, "top_5_vendors": dict(sorted(vendor_spending.items(), key=lambda x: x[1], reverse=True)[:5])}
    }

# ==================== Admin Routes ====================

@app.get("/api/admin/users")
async def list_users(current_user: User = Depends(role_required([UserRole.ADMIN])), db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "email": u.email, "full_name": u.full_name,
             "role": u.role.value, "is_active": u.is_active, "created_at": u.created_at} for u in users]

@app.post("/api/admin/users")
async def create_user(user_data: UserCreate, current_user: User = Depends(role_required([UserRole.ADMIN])),
                      db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(400, "Username already exists")
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(400, "Email already exists")
    
    user = User(username=user_data.username, email=user_data.email,
                hashed_password=get_password_hash(user_data.password),
                role=user_data.role, full_name=user_data.full_name)
    db.add(user)
    db.commit()
    db.refresh(user)
    
    return {"message": "User created", "user_id": user.id, "username": user.username, "role": user.role.value}

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc)}


# ==================== Static Files ====================

if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
else:
    print("⚠️ Static directory not found. Create a 'static' folder with your HTML files.")


# ==================== Main Entry Point ====================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("=" * 60)
    print("📄 Document Management System Starting...")
    print("=" * 60)
    print(f"🌐 Server: http://0.0.0.0:{port}")
    print(f"📚 API Docs: http://0.0.0.0:{port}/docs")
    print(f"🗄️  Database: {Config.DATABASE_TYPE.upper()}")
    print("=" * 60)
    
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
