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
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, status, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse, HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Enum as SQLEnum, ForeignKey, Boolean, Text, and_, func, or_
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from jose import JWTError, jwt
from passlib.context import CryptContext

# Document processing
import pytesseract
from PIL import Image
import pdf2image

# Data processing and reporting
import pandas as pd
import numpy as np
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# Configuration
from dotenv import load_dotenv
import uvicorn

load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this-in-production")
    if not SECRET_KEY or SECRET_KEY == "your-secret-key-change-this-in-production":
        print("⚠️ WARNING: Using default SECRET_KEY. Set a secure key in .env file!")

    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 30))

    # PostgreSQL Configuration (default for production)
    PG_HOST = os.getenv("PG_HOST", "localhost")
    PG_PORT = os.getenv("PG_PORT", "5432")
    PG_USER = os.getenv("PG_USER", "postgres")
    PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres")
    PG_DB = os.getenv("PG_DB", "doc_management")

    # MySQL fallback (optional)
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB = os.getenv("MYSQL_DB", "doc_management")

    # Database selection
    DATABASE_TYPE = os.getenv("DATABASE_TYPE", "postgresql").lower()
    
    # Render.com PostgreSQL URL
    RENDER_DATABASE_URL = os.getenv("DATABASE_URL")
    
    SQLITE_URL = "sqlite:///./doc_management.db"

    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 10 * 1024 * 1024))
    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
    
    # OpenAI Configuration
    USE_OPENAI = os.getenv("USE_OPENAI", "false").lower() == "true"
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", 0.7))

    @classmethod
    def get_database_url(cls):
        if cls.RENDER_DATABASE_URL:
            print(f"✅ Using Render.com PostgreSQL database")
            database_url = cls.RENDER_DATABASE_URL
            if database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql://", 1)
            return database_url
        
        if cls.DATABASE_TYPE == "postgresql":
            print(f"✅ Using PostgreSQL database at {cls.PG_HOST}:{cls.PG_PORT}")
            return f"postgresql://{cls.PG_USER}:{cls.PG_PASSWORD}@{cls.PG_HOST}:{cls.PG_PORT}/{cls.PG_DB}"
        elif cls.DATABASE_TYPE == "mysql":
            print(f"✅ Using MySQL database at {cls.MYSQL_HOST}:{cls.MYSQL_PORT}")
            return f"mysql+pymysql://{cls.MYSQL_USER}:{cls.MYSQL_PASSWORD}@{cls.MYSQL_HOST}:{cls.MYSQL_PORT}/{cls.MYSQL_DB}"
        else:
            print("⚠️ Using SQLite database (development only)")
            return cls.SQLITE_URL

# ==================== Database Setup ====================
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
    tax_rate = Column(Float)
    upload_date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    status = Column(SQLEnum(ApprovalStatus), default=ApprovalStatus.PENDING_LEVEL1)
    is_duplicate = Column(Boolean, default=False)
    duplicate_reason = Column(Text)
    rejection_reason = Column(Text, nullable=True)  # Added this field
    extraction_method = Column(String(50), default="ocr")
    extraction_confidence = Column(Float, default=0.0)
    openai_response = Column(Text, nullable=True)
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
    approver = relationship("User")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String(100))
    details = Column(Text)
    ip_address = Column(String(45))
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

# Create engine with proper error handling
engine = None
SessionLocal = None

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
        engine = create_engine(
            database_url, 
            pool_pre_ping=True,
            pool_recycle=3600
        )
    
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    print("✅ Database engine created successfully")
    
except Exception as e:
    print(f"❌ Database connection error: {e}")
    print("⚠️ Falling back to SQLite...")
    Config.DATABASE_TYPE = "sqlite"
    engine = create_engine(
        Config.SQLITE_URL, 
        connect_args={"check_same_thread": False}
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    print("✅ SQLite fallback engine created")

def get_db():
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
    document_type: Optional[str] = None

# ==================== Authentication ====================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)

def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, Config.SECRET_KEY, algorithm=Config.ALGORITHM)

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
):
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
    async def checker(current_user: User = Depends(get_current_user)):
        if current_user.role not in required_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return checker

# ==================== File Validation ====================
def validate_file(file_content: bytes, filename: str) -> None:
    if len(file_content) > Config.MAX_FILE_SIZE:
        raise HTTPException(400, f"Max size {Config.MAX_FILE_SIZE//1024//1024}MB")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in Config.ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Allowed extensions: {Config.ALLOWED_EXTENSIONS}")
    magic_map = {b'%PDF': '.pdf', b'\xff\xd8': '.jpg', b'\x89PNG': '.png'}
    detected = None
    for magic, ext2 in magic_map.items():
        if file_content.startswith(magic):
            detected = ext2
            break
    if detected and detected != ext:
        raise HTTPException(400, "File extension mismatch")

def generate_secure_filename(original: str) -> str:
    ext = os.path.splitext(original)[1].lower()
    return f"{uuid.uuid4().hex}{ext}"

# ==================== Document Extractor ====================
class DocumentExtractor:
    @staticmethod
    async def extract(file_path: str, document_type: str) -> Dict[str, Any]:
        """Extract data from document based on file type"""
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == '.pdf':
            return await AIExtractor.extract_from_pdf(file_path)
        elif ext in ['.jpg', '.jpeg', '.png']:
            return await AIExtractor.extract_from_image(file_path)
        else:
            return AIExtractor._get_empty_extraction()

# ==================== AI Document Extractor ====================
class AIExtractor:
    @staticmethod
    async def extract_from_image(image_path: str) -> Dict[str, Any]:
        try:
            image = Image.open(image_path)
            text = pytesseract.image_to_string(image)
            result = AIExtractor._parse_document_text(text)
            result["method"] = "ocr"
            result["confidence"] = 0.7  # Default confidence for OCR
            return result
        except Exception as e:
            print(f"OCR error: {e}")
            result = AIExtractor._get_empty_extraction()
            result["method"] = "failed"
            result["confidence"] = 0.0
            return result
    
    @staticmethod
    async def extract_from_pdf(pdf_path: str) -> Dict[str, Any]:
        try:
            images = pdf2image.convert_from_path(pdf_path, first_page=1, last_page=3)
            text = "".join(pytesseract.image_to_string(img) for img in images)
            result = AIExtractor._parse_document_text(text)
            result["method"] = "ocr"
            result["confidence"] = 0.7
            return result
        except Exception as e:
            print(f"PDF extraction error: {e}")
            result = AIExtractor._get_empty_extraction()
            result["method"] = "failed"
            result["confidence"] = 0.0
            return result
    
    @staticmethod
    def _parse_document_text(text: str) -> Dict[str, Any]:
        data = {
            "vendor_name": None,
            "invoice_number": None,
            "invoice_date": None,
            "amount": None,
            "vat_amount": None
        }
        patterns = {
            "vendor_name": [
                r"(?:Vendor|Supplier|Company|Bill From|Seller|Issuer)[\s:]+([A-Za-z0-9\s&.,]+)(?:\n|$)",
                r"^([A-Za-z0-9\s&.,]+)(?:\n|$)"
            ],
            "invoice_number": [
                r"(?:Invoice|Document|Bill)[\s#:]+(\S+)",
                r"(?:INV|INVOICE)[\s-]*(\d+)"
            ],
            "invoice_date": [
                r"(?:Date|Invoice Date|Issue Date)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
                r"(\d{4}-\d{2}-\d{2})"
            ],
            "amount": [
                r"(?:Total|Amount Due|Grand Total|Balance Due)[\s:]*[$]?([\d,]+\.?\d*)",
                r"TOTAL\s+[$]?([\d,]+\.?\d*)"
            ],
            "vat_amount": [
                r"(?:VAT|Tax|GST|HST)[\s:]*[$]?([\d,]+\.?\d*)",
                r"Tax Amount\s+[$]?([\d,]+\.?\d*)"
            ]
        }
        for key, pats in patterns.items():
            for pat in pats:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    if key == "invoice_date":
                        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
                            try:
                                data[key] = datetime.strptime(val, fmt)
                                break
                            except:
                                pass
                    elif key in ("amount", "vat_amount"):
                        try:
                            data[key] = float(val.replace(",", ""))
                        except:
                            pass
                    else:
                        data[key] = val
                    break
        return data
    
    @staticmethod
    def _get_empty_extraction():
        return {"vendor_name": None, "invoice_number": None, "invoice_date": None, "amount": None, "vat_amount": None}

# ==================== Duplicate Detection ====================
class DuplicateDetector:
    @staticmethod
    def check_duplicate(db: Session, invoice_number: Optional[str], vendor_name: Optional[str],
                        amount: Optional[float], file_content: bytes, document_type: str) -> Tuple[bool, Optional[str]]:
        file_hash = hashlib.sha256(file_content).hexdigest()
        existing_file = db.query(Document).filter(Document.file_hash == file_hash).first()
        if existing_file:
            return True, f"Duplicate file content detected (Document #{existing_file.id})"

        if invoice_number:
            existing = db.query(Document).filter(Document.invoice_number == invoice_number).first()
            if existing:
                if document_type == "credit_note" and existing.document_type == "invoice":
                    return False, None
                return True, f"Duplicate invoice number: {invoice_number} (Document #{existing.id})"

        if document_type == "invoice" and vendor_name and amount:
            thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
            dup = db.query(Document).filter(
                and_(
                    Document.vendor_name == vendor_name,
                    Document.amount == amount,
                    Document.invoice_date >= thirty_days_ago,
                    Document.document_type == "invoice"
                )
            ).first()
            if dup:
                return True, f"Possible duplicate: same vendor and amount found in document #{dup.id}"

        return False, None

# ==================== Lifespan (Startup) ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting Document Management System...")
    print(f"📊 Database Type: {Config.DATABASE_TYPE}")
    
    if engine:
        Base.metadata.create_all(bind=engine)
        print("✅ Database tables created/verified")
    
    os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
    print(f"✅ Upload directory: {Config.UPLOAD_DIR}")
    
    db = SessionLocal()
    try:
        default_users = [
            ("admin", "admin@system.com", "Admin@123", UserRole.ADMIN, "System Administrator"),
            ("approver", "approver@system.com", "Approver@123", UserRole.APPROVER, "Level1 Approver"),
            ("manager", "manager@system.com", "Manager@123", UserRole.MANAGER, "Level2 Manager"),
            ("viewer", "viewer@system.com", "Viewer@123", UserRole.VIEWER, "Report Viewer")
        ]
        for username, email, pwd, role, fullname in default_users:
            user = db.query(User).filter(User.username == username).first()
            if not user:
                user = User(
                    username=username,
                    email=email,
                    hashed_password=get_password_hash(pwd),
                    role=role,
                    full_name=fullname
                )
                db.add(user)
        db.commit()
        print("✅ Default users created")
    except Exception as e:
        print(f"⚠️ Startup error: {e}")
        db.rollback()
    finally:
        db.close()
    
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

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return RedirectResponse(url="/login.html")
    
# ==================== Authentication Routes ====================
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
        key="access_token",
        value=token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=Config.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    
    audit = AuditLog(
        user_id=user.id, 
        action="LOGIN", 
        details="User logged in", 
        ip_address=request.client.host if request.client else "unknown"
    )
    db.add(audit)
    db.commit()
    
    return {"access_token": token, "token_type": "bearer", "role": user.role.value}

@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Logged out"}

@app.get("/api/auth/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role.value
    }

# ==================== Document Routes ====================
@app.post("/api/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    document_type: str = Form(...),
    current_user: User = Depends(role_required([UserRole.ADMIN, UserRole.APPROVER, UserRole.MANAGER])),
    db: Session = Depends(get_db)
):
    """Upload invoices and credit notes only"""
    if document_type not in ["invoice", "credit_note"]:
        raise HTTPException(400, "Document type must be 'invoice' or 'credit_note'")
    
    content = await file.read()
    validate_file(content, file.filename)
    
    safe_name = generate_secure_filename(file.filename)
    file_path = os.path.join(Config.UPLOAD_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(content)
    
    file_hash = hashlib.sha256(content).hexdigest()
    
    # Extract data using AI
    extracted = await DocumentExtractor.extract(file_path, document_type)
    
    # Check for duplicates
    is_dup, dup_reason = DuplicateDetector.check_duplicate(
        db, extracted.get("invoice_number"), extracted.get("vendor_name"),
        extracted.get("amount"), content, document_type
    )
    
    doc = Document(
        filename=file.filename,
        file_path=file_path,
        file_hash=file_hash,
        document_type=document_type,
        vendor_name=extracted.get("vendor_name"),
        invoice_number=extracted.get("invoice_number"),
        invoice_date=extracted.get("invoice_date"),
        amount=extracted.get("amount"),
        vat_amount=extracted.get("vat_amount"),
        uploaded_by=current_user.id,
        status=ApprovalStatus.PENDING_LEVEL1,
        is_duplicate=is_dup,
        duplicate_reason=dup_reason,
        extraction_method=extracted.get("method", "unknown"),
        extraction_confidence=extracted.get("confidence", 0.0)
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    
    audit = AuditLog(
        user_id=current_user.id, 
        action="UPLOAD", 
        details=f"Uploaded {file.filename} (ID: {doc.id})", 
        ip_address=request.client.host if request.client else "unknown"
    )
    db.add(audit)
    db.commit()
    
    return {
        "message": "Document uploaded successfully",
        "document_id": doc.id,
        "extracted_data": {
            "vendor_name": extracted.get("vendor_name"),
            "invoice_number": extracted.get("invoice_number"),
            "invoice_date": extracted.get("invoice_date"),
            "amount": extracted.get("amount"),
            "vat_amount": extracted.get("vat_amount")
        },
        "extraction_method": extracted.get("method"),
        "extraction_confidence": extracted.get("confidence", 0),
        "is_duplicate": is_dup,
        "duplicate_reason": dup_reason,
        "status": doc.status.value
    }

@app.get("/api/documents")
async def list_documents(
    skip: int = 0,
    limit: int = 100,
    status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    query = db.query(Document)
    if current_user.role == UserRole.VIEWER:
        query = query.filter(Document.status == ApprovalStatus.APPROVED)
    if status_filter:
        query = query.filter(Document.status == status_filter)
    
    docs = query.order_by(Document.upload_date.desc()).offset(skip).limit(limit).all()
    
    return [
        {
            "id": d.id,
            "filename": d.filename,
            "document_type": d.document_type,
            "vendor_name": d.vendor_name,
            "invoice_number": d.invoice_number,
            "amount": d.amount,
            "vat_amount": d.vat_amount,
            "status": d.status.value,
            "upload_date": d.upload_date,
            "is_duplicate": d.is_duplicate,
            "extraction_method": d.extraction_method,
            "extraction_confidence": d.extraction_confidence
        }
        for d in docs
    ]

@app.get("/api/documents/{document_id}")
async def get_document(
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    
    if current_user.role == UserRole.VIEWER and doc.status != ApprovalStatus.APPROVED:
        raise HTTPException(403, "Access denied - document not approved")
    
    approvals = db.query(Approval).filter(Approval.document_id == document_id).all()
    
    return {
        "document": {
            "id": doc.id,
            "filename": doc.filename,
            "document_type": doc.document_type,
            "vendor_name": doc.vendor_name,
            "invoice_number": doc.invoice_number,
            "invoice_date": doc.invoice_date,
            "amount": doc.amount,
            "vat_amount": doc.vat_amount,
            "status": doc.status.value,
            "upload_date": doc.upload_date,
            "is_duplicate": doc.is_duplicate,
            "duplicate_reason": doc.duplicate_reason,
            "extraction_method": doc.extraction_method,
            "extraction_confidence": doc.extraction_confidence,
            "rejection_reason": doc.rejection_reason
        },
        "approval_history": [
            {
                "level": a.approval_level,
                "decision": a.decision,
                "comments": a.comments,
                "approved_at": a.approved_at,
                "approver_name": a.approver.full_name if a.approver else "Unknown"
            }
            for a in approvals
        ]
    }

# ==================== Approval Routes (3-Step Workflow) ====================
@app.post("/api/approval/process")
async def process_approval(
    action: ApprovalAction,
    request: Request,
    current_user: User = Depends(role_required([UserRole.ADMIN, UserRole.APPROVER, UserRole.MANAGER])),
    db: Session = Depends(get_db)
):
    doc = db.query(Document).filter(Document.id == action.document_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    
    if doc.status in [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]:
        raise HTTPException(400, f"Document already {doc.status.value}")
    
    # Determine which approval level this user can perform
    allowed_level = None
    if current_user.role == UserRole.APPROVER and doc.status == ApprovalStatus.PENDING_LEVEL1:
        allowed_level = 1
    elif current_user.role == UserRole.MANAGER and doc.status == ApprovalStatus.PENDING_LEVEL2:
        allowed_level = 2
    elif current_user.role == UserRole.ADMIN and doc.status == ApprovalStatus.PENDING_LEVEL3:
        allowed_level = 3
    else:
        raise HTTPException(403, f"Not authorized for this approval stage. Current status: {doc.status.value}")
    
    if action.decision not in ["approved", "rejected"]:
        raise HTTPException(400, "Decision must be 'approved' or 'rejected'")
    
    # Create approval record
    approval = Approval(
        document_id=doc.id,
        approver_id=current_user.id,
        approval_level=allowed_level,
        decision=action.decision,
        comments=action.comments
    )
    db.add(approval)
    
    # Update document status based on decision
    if action.decision == "rejected":
        doc.status = ApprovalStatus.REJECTED
        doc.rejection_reason = action.comments
        message = f"Document #{doc.id} REJECTED at level {allowed_level} by {current_user.role.value}"
    else:
        # Move to next level or approve
        if allowed_level == 1:
            doc.status = ApprovalStatus.PENDING_LEVEL2
            message = f"Document #{doc.id} approved at Level 1, now pending Level 2 (Manager) approval"
        elif allowed_level == 2:
            doc.status = ApprovalStatus.PENDING_LEVEL3
            message = f"Document #{doc.id} approved at Level 2, now pending Level 3 (Admin/Finance) approval"
        else:  # Level 3 approval
            doc.status = ApprovalStatus.APPROVED
            message = f"Document #{doc.id} FULLY APPROVED"
    
    db.commit()
    
    audit = AuditLog(
        user_id=current_user.id, 
        action="APPROVAL", 
        details=f"Document {doc.id} {action.decision} at level {allowed_level}", 
        ip_address=request.client.host if request.client else "unknown"
    )
    db.add(audit)
    db.commit()
    
    return {
        "message": message,
        "document_id": doc.id,
        "status": doc.status.value,
        "approval_level": allowed_level,
        "decision": action.decision
    }

@app.get("/api/approval/pending")
async def get_pending_approvals(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get documents pending approval for the current user's role"""
    if current_user.role == UserRole.APPROVER:
        docs = db.query(Document).filter(Document.status == ApprovalStatus.PENDING_LEVEL1).all()
    elif current_user.role == UserRole.MANAGER:
        docs = db.query(Document).filter(Document.status == ApprovalStatus.PENDING_LEVEL2).all()
    elif current_user.role == UserRole.ADMIN:
        docs = db.query(Document).filter(Document.status == ApprovalStatus.PENDING_LEVEL3).all()
    else:
        docs = []
    
    return [
        {
            "id": d.id,
            "filename": d.filename,
            "document_type": d.document_type,
            "vendor_name": d.vendor_name,
            "invoice_number": d.invoice_number,
            "amount": d.amount,
            "status": d.status.value,
            "upload_date": d.upload_date,
            "extraction_confidence": d.extraction_confidence
        }
        for d in docs
    ]

# ==================== Report Routes ====================
@app.post("/api/reports/spend-summary")
async def spend_summary(
    filters: ReportFilter,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Spend summary report with various filters"""
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)

    if filters.start_date:
        query = query.filter(Document.invoice_date >= filters.start_date)
    if filters.end_date:
        query = query.filter(Document.invoice_date <= filters.end_date)
    if filters.vendor_name:
        query = query.filter(Document.vendor_name.ilike(f"%{filters.vendor_name}%"))
    if filters.status:
        query = query.filter(Document.status == filters.status)
    if filters.min_amount is not None:
        query = query.filter(Document.amount >= filters.min_amount)
    if filters.max_amount is not None:
        query = query.filter(Document.amount <= filters.max_amount)
    if filters.document_type:
        query = query.filter(Document.document_type == filters.document_type)

    docs = query.all()
    if not docs:
        return {"message": "No documents found matching criteria"}

    total_amount = sum(d.amount or 0 for d in docs)
    total_vat = sum(d.vat_amount or 0 for d in docs)
    
    vendor_breakdown = {}
    for d in docs:
        if d.vendor_name:
            vendor_breakdown[d.vendor_name] = vendor_breakdown.get(d.vendor_name, 0) + (d.amount or 0)
    
    monthly_trend = {}
    for d in docs:
        if d.invoice_date:
            month_key = d.invoice_date.strftime("%Y-%m")
            monthly_trend[month_key] = monthly_trend.get(month_key, 0) + (d.amount or 0)
    
    status_counts = {s.value: 0 for s in ApprovalStatus}
    for d in docs:
        status_counts[d.status.value] += 1

    return {
        "summary": {
            "total_amount": total_amount,
            "total_vat": total_vat,
            "total_without_vat": total_amount - total_vat,
            "document_count": len(docs),
            "unique_vendors": len(vendor_breakdown),
            "average_amount": total_amount / len(docs) if docs else 0
        },
        "vendor_breakdown": vendor_breakdown,
        "monthly_trend": monthly_trend,
        "status_overview": status_counts,
        "documents": [
            {
                "id": d.id,
                "vendor": d.vendor_name,
                "invoice_number": d.invoice_number,
                "date": d.invoice_date,
                "amount": d.amount,
                "vat": d.vat_amount,
                "status": d.status.value
            }
            for d in docs[:100]
        ]
    }

@app.post("/api/reports/tax")
async def tax_report(
    filters: ReportFilter,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Tax/VAT report"""
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)
    
    if filters.start_date:
        query = query.filter(Document.invoice_date >= filters.start_date)
    if filters.end_date:
        query = query.filter(Document.invoice_date <= filters.end_date)
    if filters.vendor_name:
        query = query.filter(Document.vendor_name.ilike(f"%{filters.vendor_name}%"))
    
    docs = query.all()
    total_taxable = sum(d.amount or 0 for d in docs)
    total_vat = sum(d.vat_amount or 0 for d in docs)
    
    vendor_tax = {}
    for d in docs:
        if d.vendor_name:
            vendor_tax[d.vendor_name] = vendor_tax.get(d.vendor_name, 0) + (d.vat_amount or 0)
    
    return {
        "period": {"start_date": filters.start_date, "end_date": filters.end_date},
        "summary": {
            "total_taxable_amount": total_taxable,
            "total_vat_collected": total_vat,
            "effective_tax_rate": (total_vat / total_taxable * 100) if total_taxable > 0 else 0,
            "transaction_count": len(docs)
        },
        "vendor_tax_breakdown": vendor_tax,
        "transactions": [
            {
                "id": d.id,
                "vendor": d.vendor_name,
                "invoice_number": d.invoice_number,
                "date": d.invoice_date,
                "taxable_amount": d.amount,
                "vat_amount": d.vat_amount,
                "tax_rate": ((d.vat_amount / d.amount) * 100) if d.amount and d.vat_amount else 0
            }
            for d in docs[:100]
        ]
    }

# ==================== Export Routes ====================
@app.get("/api/reports/export/excel")
async def export_excel(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    vendor_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    min_amount: Optional[float] = Query(None),
    max_amount: Optional[float] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export report to Excel"""
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)
    
    if start_date:
        query = query.filter(Document.invoice_date >= start_date)
    if end_date:
        query = query.filter(Document.invoice_date <= end_date)
    if vendor_name:
        query = query.filter(Document.vendor_name.ilike(f"%{vendor_name}%"))
    if status:
        query = query.filter(Document.status == status)
    if min_amount is not None:
        query = query.filter(Document.amount >= min_amount)
    if max_amount is not None:
        query = query.filter(Document.amount <= max_amount)

    docs = query.all()
    data = []
    for d in docs:
        data.append({
            "Document ID": d.id,
            "Filename": d.filename,
            "Type": d.document_type,
            "Vendor": d.vendor_name or "",
            "Invoice Number": d.invoice_number or "",
            "Date": d.invoice_date,
            "Amount": d.amount or 0,
            "VAT Amount": d.vat_amount or 0,
            "Status": d.status.value,
            "Upload Date": d.upload_date,
            "Extraction Method": d.extraction_method,
            "Extraction Confidence": f"{d.extraction_confidence*100:.1f}%" if d.extraction_confidence else "N/A",
            "Is Duplicate": "Yes" if d.is_duplicate else "No"
        })
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Documents', index=False)
        if data:
            summary = pd.DataFrame([
                ["Total Amount", df["Amount"].sum()],
                ["Total VAT", df["VAT Amount"].sum()],
                ["Number of Documents", len(df)],
                ["Unique Vendors", df["Vendor"].nunique()],
                ["Average Amount", df["Amount"].mean()]
            ], columns=["Metric", "Value"])
            summary.to_excel(writer, sheet_name='Summary', index=False)
    
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"}
    )

@app.get("/api/reports/export/pdf")
async def export_pdf(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    vendor_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    min_amount: Optional[float] = Query(None),
    max_amount: Optional[float] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Export report to PDF"""
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)
    
    if start_date:
        query = query.filter(Document.invoice_date >= start_date)
    if end_date:
        query = query.filter(Document.invoice_date <= end_date)
    if vendor_name:
        query = query.filter(Document.vendor_name.ilike(f"%{vendor_name}%"))
    if status:
        query = query.filter(Document.status == status)
    if min_amount is not None:
        query = query.filter(Document.amount >= min_amount)
    if max_amount is not None:
        query = query.filter(Document.amount <= max_amount)

    docs = query.limit(100).all()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    elements = []
    
    title = Paragraph(f"Document Management Report - {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 12))
    
    total_amount = sum(d.amount or 0 for d in docs)
    elements.append(Paragraph(f"<b>Total Amount:</b> ${total_amount:,.2f}", styles['Normal']))
    elements.append(Paragraph(f"<b>Number of Documents:</b> {len(docs)}", styles['Normal']))
    elements.append(Spacer(1, 12))
    
    table_data = [["ID", "Vendor", "Invoice #", "Date", "Amount", "VAT", "Status"]]
    for d in docs:
        table_data.append([
            str(d.id),
            (d.vendor_name or "")[:30],
            (d.invoice_number or "")[:20],
            d.invoice_date.strftime("%Y-%m-%d") if d.invoice_date else "",
            f"${d.amount:,.2f}" if d.amount else "$0.00",
            f"${d.vat_amount:,.2f}" if d.vat_amount else "$0.00",
            d.status.value[:15]
        ])
    
    table = Table(table_data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.grey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 10),
        ('BOTTOMPADDING', (0,0), (-1,0), 12),
        ('BACKGROUND', (0,1), (-1,-1), colors.beige),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('FONTSIZE', (0,1), (-1,-1), 8),
    ]))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"}
    )

# ==================== Analytics Routes ====================
@app.get("/api/analytics/insights")
async def get_ai_insights(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """AI-driven insights on spending patterns, trends, and anomalies"""
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=365)
    
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(
            and_(Document.status == ApprovalStatus.APPROVED, Document.invoice_date >= start_date)
        )
    else:
        query = db.query(Document).filter(Document.invoice_date >= start_date)
    
    docs = query.all()
    if len(docs) < 5:
        return {"message": "Insufficient data for analysis (need at least 5 documents)"}
    
    amounts = [d.amount for d in docs if d.amount]
    monthly_spending = {}
    vendor_spending = {}
    
    for d in docs:
        if d.invoice_date and d.amount:
            month_key = d.invoice_date.strftime("%Y-%m")
            monthly_spending[month_key] = monthly_spending.get(month_key, 0) + d.amount
        if d.vendor_name and d.amount:
            vendor_spending[d.vendor_name] = vendor_spending.get(d.vendor_name, 0) + d.amount
    
    # Detect anomalies (transactions > 2 standard deviations from mean)
    anomalies = []
    if len(amounts) > 1:
        mean_amt = np.mean(amounts)
        std_amt = np.std(amounts)
        for d in docs:
            if d.amount and d.amount > mean_amt + 2 * std_amt:
                anomalies.append({
                    "document_id": d.id,
                    "vendor": d.vendor_name,
                    "amount": d.amount,
                    "date": d.invoice_date,
                    "reason": f"Amount is {((d.amount - mean_amt) / std_amt):.1f} standard deviations above mean"
                })
    
    # Generate insights
    insights = []
    monthly_values = list(monthly_spending.values())
    if len(monthly_values) >= 2:
        change = ((monthly_values[-1] - monthly_values[-2]) / monthly_values[-2] * 100) if monthly_values[-2] > 0 else 0
        if change > 10:
            insights.append(f"📈 Significant spending increase of {change:.1f}% compared to last month")
        elif change < -10:
            insights.append(f"📉 Significant spending decrease of {abs(change):.1f}% compared to last month")
        elif change != 0:
            insights.append(f"📊 Spending changed by {change:.1f}% compared to last month")
    
    top_vendors = sorted(vendor_spending.items(), key=lambda x: x[1], reverse=True)[:3]
    if top_vendors:
        vendors_text = ", ".join([f"{v} (${a:,.0f})" for v, a in top_vendors])
        insights.append(f"🏢 Top 3 vendors by spending: {vendors_text}")
    
    avg_amount = np.mean(amounts) if amounts else 0
    insights.append(f"💰 Average transaction amount: ${avg_amount:,.2f}")
    
    if monthly_spending:
        highest_month = max(monthly_spending, key=monthly_spending.get)
        insights.append(f"📅 Highest spending month: {highest_month} (${monthly_spending[highest_month]:,.2f})")
        
        lowest_month = min(monthly_spending, key=monthly_spending.get)
        if lowest_month != highest_month:
            insights.append(f"📅 Lowest spending month: {lowest_month} (${monthly_spending[lowest_month]:,.2f})")
    
    total_vat = sum(d.vat_amount or 0 for d in docs)
    if total_vat > 0:
        insights.append(f"🧾 Total VAT collected: ${total_vat:,.2f}")
    
    # Duplicate insights
    duplicate_count = sum(1 for d in docs if d.is_duplicate)
    if duplicate_count > 0:
        insights.append(f"⚠️ Found {duplicate_count} duplicate documents - review your upload process")
    
    # Approval insights
    pending_count = sum(1 for d in docs if d.status in [ApprovalStatus.PENDING_LEVEL1, ApprovalStatus.PENDING_LEVEL2, ApprovalStatus.PENDING_LEVEL3])
    if pending_count > 5:
        insights.append(f"⏳ {pending_count} documents pending approval - action required")
    
    return {
        "insights": insights,
        "anomalies": anomalies[:10],
        "statistics": {
            "total_spending": sum(amounts),
            "average_transaction": np.mean(amounts) if amounts else 0,
            "median_transaction": np.median(amounts) if amounts else 0,
            "total_transactions": len(docs),
            "unique_vendors": len(vendor_spending),
            "total_vat": total_vat,
            "duplicate_count": duplicate_count,
            "pending_approvals": pending_count
        },
        "trends": {
            "monthly_spending": monthly_spending,
            "top_5_vendors": dict(sorted(vendor_spending.items(), key=lambda x: x[1], reverse=True)[:5])
        }
    }

@app.get("/api/analytics/forecast")
async def get_spending_forecast(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Forecast future spending based on historical data"""
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=180)
    
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(
            and_(Document.status == ApprovalStatus.APPROVED, Document.invoice_date >= start_date, Document.amount.isnot(None))
        )
    else:
        query = db.query(Document).filter(Document.invoice_date >= start_date, Document.amount.isnot(None))
    
    docs = query.all()
    if len(docs) < 10:
        return {"message": "Insufficient data for forecasting (need at least 10 transactions)"}
    
    monthly_totals = {}
    for d in docs:
        if d.invoice_date and d.amount:
            month_key = d.invoice_date.strftime("%Y-%m")
            monthly_totals[month_key] = monthly_totals.get(month_key, 0) + d.amount
    
    monthly_values = list(monthly_totals.values())
    if len(monthly_values) >= 3:
        forecast = np.mean(monthly_values[-3:])
        confidence_interval = np.std(monthly_values[-3:]) * 1.96
        trend = "increasing" if monthly_values[-1] > monthly_values[-2] else "decreasing" if len(monthly_values) > 1 else "stable"
        
        return {
            "forecast_next_month": round(forecast, 2),
            "confidence_interval": {
                "lower": round(max(0, forecast - confidence_interval), 2),
                "upper": round(forecast + confidence_interval, 2)
            },
            "trend": trend,
            "data_points": len(monthly_values),
            "historical_average": round(np.mean(monthly_values), 2),
            "recommendation": "Budget accordingly for next month based on increasing trend" if forecast > np.mean(monthly_values) else "Expected spending to remain stable or decrease"
        }
    else:
        return {"message": f"Need at least 3 months of data for forecast. Currently have {len(monthly_values)} months."}

# ==================== Admin Routes ====================
@app.get("/api/admin/users")
async def list_users(
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    users = db.query(User).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "full_name": u.full_name,
            "role": u.role.value,
            "is_active": u.is_active,
            "created_at": u.created_at
        }
        for u in users
    ]

@app.post("/api/admin/users")
async def create_user(
    user_data: UserCreate,
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(400, "Username already exists")
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(400, "Email already exists")
    
    user = User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=get_password_hash(user_data.password),
        role=user_data.role,
        full_name=user_data.full_name
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    return {"message": "User created", "user_id": user.id, "username": user.username, "role": user.role.value}

@app.get("/api/admin/audit-logs")
async def get_audit_logs(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).offset(skip).limit(limit).all()
    total = db.query(AuditLog).count()
    
    return {
        "total": total,
        "logs": [
            {
                "id": log.id,
                "user_id": log.user_id,
                "action": log.action,
                "details": log.details,
                "timestamp": log.timestamp
            }
            for log in logs
        ]
    }

@app.get("/api/system/extraction-stats")
async def get_extraction_stats(
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    stats = db.query(
        Document.extraction_method,
        func.count(Document.id).label('count'),
        func.avg(Document.extraction_confidence).label('avg_confidence')
    ).group_by(Document.extraction_method).all()
    
    return {
        "extraction_stats": [
            {
                "method": stat[0],
                "count": stat[1],
                "avg_confidence": float(stat[2]) if stat[2] else 0
            }
            for stat in stats
        ],
        "total_documents": db.query(Document).count(),
        "low_confidence_documents": db.query(Document).filter(Document.extraction_confidence < Config.CONFIDENCE_THRESHOLD).count(),
        "openai_enabled": Config.USE_OPENAI and bool(Config.OPENAI_API_KEY),
        "openai_model": Config.OPENAI_MODEL if Config.USE_OPENAI else None
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc),
        "openai_configured": bool(Config.OPENAI_API_KEY),
        "openai_enabled": Config.USE_OPENAI
    }

# ==================== Static Files ====================

if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
else:
    print("⚠️ Static directory not found. Create a 'static' folder with your HTML files.")

# ==================== Main ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("=" * 60)
    print("📄 Document Management System Starting...")
    print("=" * 60)
    print(f"🌐 Server: http://0.0.0.0:{port}")
    print(f"📚 API Docs: http://0.0.0.0:{port}/docs")
    print(f"🗄️  Database: {Config.get_database_url()}")
    print(f"🤖 OpenAI: {'Enabled' if Config.USE_OPENAI and Config.OPENAI_API_KEY else 'Disabled'}")
    print("=" * 60)
    print("Default Login Credentials:")
    print("  👑 Admin:    admin / Admin@123")
    print("  ✅ Approver: approver / Approver@123")
    print("  📊 Manager:  manager / Manager@123")
    print("  👁️ Viewer:   viewer / Viewer@123")
    print("=" * 60)
    
    uvicorn.run(
        "app:app", 
        host="0.0.0.0", 
        port=port, 
        reload=False
    )
