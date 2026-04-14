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
from fastapi.responses import StreamingResponse, RedirectResponse
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
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
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
    DATABASE_TYPE = os.getenv("DATABASE_TYPE", "postgresql").lower()  # postgresql, mysql, sqlite
    
    # Render.com PostgreSQL URL (automatically provided)
    RENDER_DATABASE_URL = os.getenv("DATABASE_URL")
    
    SQLITE_URL = "sqlite:///./doc_management.db"

    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 10 * 1024 * 1024))
    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

    @classmethod
    def get_database_url(cls):
        # Priority 1: Use Render.com DATABASE_URL if available (PostgreSQL)
        if cls.RENDER_DATABASE_URL:
            print(f"✅ Using Render.com PostgreSQL database")
            # Convert postgres:// to postgresql:// for SQLAlchemy
            database_url = cls.RENDER_DATABASE_URL
            if database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql://", 1)
            return database_url
        
        # Priority 2: Use specified DATABASE_TYPE
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

# Create engine with proper error handling
engine = None
SessionLocal = None

try:
    database_url = Config.get_database_url()

    if "postgresql" in database_url:
        # PostgreSQL specific configuration
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            pool_recycle=3600,
            echo=False
        )
    else:  # MySQL
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

# ==================== Lifespan (Startup) ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting Document Management System...")
    print(f"📊 Database Type: {Config.DATABASE_TYPE}")
    
    # Create tables
    if engine:
        Base.metadata.create_all(bind=engine)
        print("✅ Database tables created/verified")
    
    # Create upload directory
    os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
    print(f"✅ Upload directory: {Config.UPLOAD_DIR}")
    
    # Create default users
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
# Add these imports at the top
import aiohttp
import asyncio
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
import json
import openai  # pip install openai
from transformers import pipeline  # pip install transformers torch

# ==================== Enhanced AI Extraction ====================

@dataclass
class ExtractedData:
    """Structured data extracted from documents"""
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    amount: Optional[float] = None
    vat_amount: Optional[float] = None
    tax_rate: Optional[float] = None
    po_number: Optional[str] = None
    line_items: List[Dict] = None
    confidence_scores: Dict[str, float] = None
    
    def __post_init__(self):
        if self.line_items is None:
            self.line_items = []
        if self.confidence_scores is None:
            self.confidence_scores = {}

class AdvancedAIExtractor:
    """Multi-strategy AI extraction using OCR + LLM"""
    
    def __init__(self, use_openai: bool = False, openai_api_key: str = None):
        self.use_openai = use_openai
        if use_openai and openai_api_key:
            openai.api_key = openai_api_key
        self.ocr_engine = pytesseract
        
        # Optional: Use local transformer model for NER
        try:
            self.ner_pipeline = pipeline("ner", model="dslim/bert-base-NER", aggregation_strategy="simple")
        except:
            self.ner_pipeline = None
            print("⚠️ Transformers NER not available")
    
    async def extract_from_image(self, image_path: str) -> ExtractedData:
        """Extract data from image using OCR + AI"""
        try:
            # Step 1: OCR extraction
            image = Image.open(image_path)
            
            # Preprocess image for better OCR
            image = self._preprocess_image(image)
            
            # Get raw text with confidence
            ocr_data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
            raw_text = " ".join(ocr_data['text'])
            
            # Step 2: Extract using regex patterns
            regex_data = self._extract_with_regex(raw_text)
            
            # Step 3: If OpenAI is enabled, enhance extraction
            if self.use_openai:
                llm_data = await self._extract_with_llm(raw_text)
                # Merge with priority to LLM
                regex_data = self._merge_extractions(regex_data, llm_data)
            
            # Step 4: Extract line items if present
            line_items = await self._extract_line_items(raw_text)
            
            # Step 5: Calculate confidence scores
            confidence = self._calculate_confidence(regex_data, raw_text)
            
            return ExtractedData(
                vendor_name=regex_data.get("vendor_name"),
                invoice_number=regex_data.get("invoice_number"),
                invoice_date=regex_data.get("invoice_date"),
                due_date=regex_data.get("due_date"),
                amount=regex_data.get("amount"),
                vat_amount=regex_data.get("vat_amount"),
                tax_rate=regex_data.get("tax_rate"),
                po_number=regex_data.get("po_number"),
                line_items=line_items,
                confidence_scores=confidence
            )
            
        except Exception as e:
            print(f"Extraction error: {e}")
            return ExtractedData()
    
    async def extract_from_pdf(self, pdf_path: str, max_pages: int = 5) -> ExtractedData:
        """Extract data from PDF with multi-page support"""
        try:
            # Convert first few pages to images
            images = pdf2image.convert_from_path(pdf_path, first_page=1, last_page=max_pages)
            
            all_text = []
            all_extractions = []
            
            for page_num, image in enumerate(images, 1):
                # Process each page
                page_data = await self.extract_from_image(image)
                all_text.append(page_data)
                
                # Extract data from this page
                extracted = await self._extract_from_image_page(image)
                all_extractions.append(extracted)
            
            # Merge extractions from all pages
            merged = self._merge_multi_page_extractions(all_extractions)
            
            # Full text for context
            full_text = " ".join([str(t) for t in all_text])
            
            return merged
            
        except Exception as e:
            print(f"PDF extraction error: {e}")
            return ExtractedData()
    
    def _preprocess_image(self, image: Image.Image) -> Image.Image:
        """Preprocess image for better OCR accuracy"""
        # Convert to grayscale
        if image.mode != 'L':
            image = image.convert('L')
        
        # Increase contrast
        from PIL import ImageEnhance
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0)
        
        # Denoise
        from PIL import ImageFilter
        image = image.filter(ImageFilter.MedianFilter())
        
        return image
    
    async def _extract_with_llm(self, text: str) -> Dict[str, Any]:
        """Use LLM for intelligent extraction"""
        if not self.use_openai:
            return {}
        
        prompt = f"""
        Extract the following information from this invoice document.
        Return ONLY a JSON object with these fields (null if not found):
        - vendor_name: The company name of the seller/supplier
        - invoice_number: The invoice/document number
        - invoice_date: Date in YYYY-MM-DD format
        - due_date: Payment due date in YYYY-MM-DD format
        - amount: Total amount due (numeric)
        - vat_amount: VAT/tax amount (numeric)
        - tax_rate: Tax rate percentage (numeric)
        - po_number: Purchase order number if present
        
        Document text:
        {text[:3000]}  # Limit text length
        
        JSON Output:
        """
        
        try:
            response = await openai.ChatCompletion.acreate(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are an expert invoice data extractor."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=500
            )
            
            result = json.loads(response.choices[0].message.content)
            return result
            
        except Exception as e:
            print(f"LLM extraction error: {e}")
            return {}
    
    async def _extract_from_image_page(self, image: Image.Image) -> Dict[str, Any]:
        """Extract from a single image page"""
        text = pytesseract.image_to_string(image)
        return self._extract_with_regex(text)
    
    def _extract_with_regex(self, text: str) -> Dict[str, Any]:
        """Enhanced regex extraction with multiple patterns"""
        data = {}
        
        # Enhanced vendor patterns
        vendor_patterns = [
            r"(?:Vendor|Supplier|Company|Bill From|Seller|Issuer|From)[\s:]+([A-Za-z0-9\s&.,]+)(?:\n|$)",
            r"^([A-Za-z0-9\s&.,]+)(?:\n|$)",
            r"Invoice\s+from:\s*([^\n]+)",
            r"Bill\s+to:\s*([^\n]+)"
        ]
        
        # Enhanced invoice number patterns
        inv_patterns = [
            r"(?:Invoice|Document|Bill)[\s#:]+(\S+)",
            r"(?:INV|INVOICE)[\s-]*(\d+)",
            r"Invoice\s+Number:\s*(\S+)",
            r"Document\s+ID:\s*(\S+)"
        ]
        
        # Date patterns with multiple formats
        date_patterns = [
            (r"(?:Date|Invoice Date|Issue Date)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", ["%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y"]),
            (r"(\d{4}-\d{2}-\d{2})", ["%Y-%m-%d"]),
            (r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})", ["%d %b %Y", "%d %B %Y"])
        ]
        
        # Amount patterns with currency support
        amount_patterns = [
            r"(?:Total|Amount Due|Grand Total|Balance Due)[\s:]*[$€£]?([\d,]+\.?\d*)",
            r"TOTAL\s+[$€£]?([\d,]+\.?\d*)",
            r"Amount\s+Due:\s*[$€£]?([\d,]+\.?\d*)",
            r"Net\s+Total:\s*[$€£]?([\d,]+\.?\d*)"
        ]
        
        # Extract vendor
        for pattern in vendor_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                data["vendor_name"] = match.group(1).strip()
                break
        
        # Extract invoice number
        for pattern in inv_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                data["invoice_number"] = match.group(1).strip()
                break
        
        # Extract dates
        for pattern, formats in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                for fmt in formats:
                    try:
                        date_obj = datetime.strptime(date_str, fmt)
                        if "Due" in pattern or "due" in pattern:
                            data["due_date"] = date_obj
                        else:
                            data["invoice_date"] = date_obj
                        break
                    except:
                        pass
        
        # Extract amounts
        for pattern in amount_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    amount = float(match.group(1).replace(",", ""))
                    if "VAT" in pattern or "Tax" in pattern:
                        data["vat_amount"] = amount
                    else:
                        data["amount"] = amount
                except:
                    pass
        
        # Extract tax rate
        tax_pattern = r"(?:VAT|Tax|GST|HST)[\s:]+(\d+(?:\.\d+)?)%"
        match = re.search(tax_pattern, text, re.IGNORECASE)
        if match:
            data["tax_rate"] = float(match.group(1))
        
        # Extract PO number
        po_pattern = r"(?:PO|Purchase Order)[\s#:]+(\S+)"
        match = re.search(po_pattern, text, re.IGNORECASE)
        if match:
            data["po_number"] = match.group(1)
        
        return data
    
    async def _extract_line_items(self, text: str) -> List[Dict]:
        """Extract line items from invoice"""
        line_items = []
        
        # Look for table-like structures
        lines = text.split('\n')
        in_table = False
        table_headers = []
        
        for line in lines:
            # Detect table start
            if re.search(r'(Item|Description|Quantity|Qty|Unit Price|Amount)', line, re.IGNORECASE):
                in_table = True
                # Extract headers
                table_headers = re.findall(r'\b(\w+(?:\s+\w+)?)\b', line)
                continue
            
            if in_table and line.strip():
                # Parse row
                numbers = re.findall(r'[\d,]+\.?\d*', line)
                if len(numbers) >= 2:
                    item = {
                        "description": re.sub(r'[\d,]+\.?\d*', '', line).strip(),
                        "quantity": float(numbers[0]) if len(numbers) > 0 else None,
                        "unit_price": float(numbers[1]) if len(numbers) > 1 else None,
                        "total": float(numbers[-1]) if numbers else None
                    }
                    line_items.append(item)
                
                # Stop table after certain lines
                if len(line_items) > 20:
                    break
        
        return line_items
    
    def _merge_extractions(self, primary: Dict, secondary: Dict) -> Dict:
        """Merge extractions with priority to secondary (LLM)"""
        merged = primary.copy()
        for key, value in secondary.items():
            if value is not None and value != "":
                merged[key] = value
        return merged
    
    def _merge_multi_page_extractions(self, extractions: List[Dict]) -> ExtractedData:
        """Merge data from multiple pages"""
        merged = {}
        
        for extraction in extractions:
            for key, value in extraction.items():
                if value and not merged.get(key):
                    merged[key] = value
        
        return ExtractedData(**merged)
    
    def _calculate_confidence(self, extracted: Dict, text: str) -> Dict[str, float]:
        """Calculate confidence scores for each extracted field"""
        confidence = {}
        
        # Check if field exists in text
        for field, value in extracted.items():
            if value:
                # Convert value to string for checking
                value_str = str(value)
                if value_str.lower() in text.lower():
                    confidence[field] = 0.9
                elif any(word in text.lower() for word in value_str.lower().split()):
                    confidence[field] = 0.7
                else:
                    confidence[field] = 0.5
            else:
                confidence[field] = 0.0
        
        return confidence

# ==================== Enhanced AI Duplicate Detection ====================

class AdvancedDuplicateDetector:
    """Multi-strategy duplicate detection using AI"""
    
    def __init__(self):
        self.similarity_threshold = 0.85
        # Optional: Use sentence transformers for semantic similarity
        try:
            from sentence_transformers import SentenceTransformer
            self.semantic_model = SentenceTransformer('all-MiniLM-L6-v2')
            self.use_semantic = True
        except:
            self.use_semantic = False
            print("⚠️ Sentence-transformers not available for semantic duplicate detection")
    
    async def check_duplicate_advanced(
        self, 
        db: Session, 
        extracted_data: ExtractedData,
        file_content: bytes,
        document_type: str
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Advanced duplicate detection with multiple strategies
        Returns: (is_duplicate, reason, similarity_scores)
        """
        similarity_scores = {}
        
        # 1. Exact hash match (100% confidence)
        file_hash = hashlib.sha256(file_content).hexdigest()
        existing_by_hash = db.query(Document).filter(Document.file_hash == file_hash).first()
        if existing_by_hash:
            return True, f"Exact duplicate file (Document #{existing_by_hash.id})", {"hash": 1.0}
        
        # 2. Invoice number match with fuzzy matching
        if extracted_data.invoice_number:
            existing_by_inv = db.query(Document).filter(
                Document.invoice_number.isnot(None)
            ).all()
            
            for existing in existing_by_inv:
                similarity = self._fuzzy_match(
                    extracted_data.invoice_number, 
                    existing.invoice_number
                )
                if similarity > self.similarity_threshold:
                    return True, f"Similar invoice number: {extracted_data.invoice_number} (Document #{existing.id}, similarity: {similarity:.2%})", {"invoice_number": similarity}
        
        # 3. Semantic similarity for vendor + amount + date
        if extracted_data.vendor_name and extracted_data.amount:
            # Query similar documents in last 90 days
            ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)
            similar_docs = db.query(Document).filter(
                and_(
                    Document.upload_date >= ninety_days_ago,
                    Document.vendor_name.isnot(None),
                    Document.amount.isnot(None)
                )
            ).all()
            
            for existing in similar_docs:
                # Calculate composite similarity
                vendor_sim = self._fuzzy_match(
                    extracted_data.vendor_name, 
                    existing.vendor_name or ""
                )
                
                amount_sim = 1.0 - min(
                    abs((extracted_data.amount - (existing.amount or 0)) / max(extracted_data.amount, 1)),
                    1.0
                )
                
                date_sim = 1.0
                if extracted_data.invoice_date and existing.invoice_date:
                    days_diff = abs((extracted_data.invoice_date - existing.invoice_date).days)
                    date_sim = max(0, 1 - (days_diff / 30))
                
                # Weighted similarity
                composite_sim = (vendor_sim * 0.5) + (amount_sim * 0.3) + (date_sim * 0.2)
                
                if composite_sim > self.similarity_threshold:
                    return True, f"Potential duplicate with similar vendor/amount (Document #{existing.id}, similarity: {composite_sim:.2%})", {
                        "composite": composite_sim,
                        "vendor": vendor_sim,
                        "amount": amount_sim,
                        "date": date_sim
                    }
        
        # 4. Semantic content similarity (if using ML)
        if self.use_semantic and extracted_data.vendor_name:
            # This would require storing embeddings of documents
            pass
        
        # 5. Credit note - invoice relationship check
        if document_type == "credit_note" and extracted_data.invoice_number:
            original_invoice = db.query(Document).filter(
                Document.invoice_number == extracted_data.invoice_number,
                Document.document_type == "invoice"
            ).first()
            
            if original_invoice:
                similarity_scores["credit_note_match"] = 1.0
                # Not a duplicate, but a related document
                return False, f"Credit note referencing invoice #{extracted_data.invoice_number}", similarity_scores
        
        return False, None, similarity_scores
    
    def _fuzzy_match(self, str1: str, str2: str) -> float:
        """Calculate fuzzy string similarity"""
        if not str1 or not str2:
            return 0.0
        
        # Normalize strings
        str1 = str1.lower().strip()
        str2 = str2.lower().strip()
        
        # Exact match
        if str1 == str2:
            return 1.0
        
        # Length-based similarity
        len_sim = 1 - abs(len(str1) - len(str2)) / max(len(str1), len(str2))
        
        # Character set similarity
        set1 = set(str1)
        set2 = set(str2)
        jaccard = len(set1 & set2) / len(set1 | set2) if (set1 | set2) else 0
        
        # Word overlap
        words1 = set(str1.split())
        words2 = set(str2.split())
        word_jaccard = len(words1 & words2) / len(words1 | words2) if (words1 | words2) else 0
        
        # Combined similarity
        similarity = (len_sim * 0.3) + (jaccard * 0.3) + (word_jaccard * 0.4)
        
        return similarity
    
    async def find_similar_documents(
        self, 
        db: Session, 
        document_id: int,
        limit: int = 5
    ) -> List[Dict]:
        """Find documents similar to given document"""
        document = db.query(Document).filter(Document.id == document_id).first()
        if not document:
            return []
        
        similar_docs = []
        
        # Find by vendor name
        if document.vendor_name:
            vendor_matches = db.query(Document).filter(
                and_(
                    Document.id != document_id,
                    Document.vendor_name.ilike(f"%{document.vendor_name}%")
                )
            ).limit(limit).all()
            
            for match in vendor_matches:
                similarity = self._fuzzy_match(document.vendor_name, match.vendor_name or "")
                similar_docs.append({
                    "document_id": match.id,
                    "similarity": similarity,
                    "reason": "Same vendor",
                    "document": match
                })
        
        # Find by amount range
        if document.amount:
            amount_range = document.amount * 0.1  # 10% range
            amount_matches = db.query(Document).filter(
                and_(
                    Document.id != document_id,
                    Document.amount.between(
                        document.amount - amount_range,
                        document.amount + amount_range
                    )
                )
            ).limit(limit).all()
            
            for match in amount_matches:
                similarity = 1 - abs(document.amount - (match.amount or 0)) / document.amount
                similar_docs.append({
                    "document_id": match.id,
                    "similarity": similarity,
                    "reason": "Similar amount",
                    "document": match
                })
        
        # Sort by similarity and remove duplicates
        unique_docs = {}
        for doc in sorted(similar_docs, key=lambda x: x["similarity"], reverse=True):
            if doc["document_id"] not in unique_docs:
                unique_docs[doc["document_id"]] = doc
        
        return list(unique_docs.values())[:limit]

# ==================== Update Document Upload Endpoint ====================

# Replace the existing upload_document function with this enhanced version
@app.post("/api/documents/upload-enhanced")
async def upload_document_enhanced(
    request: Request,
    file: UploadFile = File(...),
    document_type: str = Form(...),
    use_ai_enhancement: bool = Form(False),
    current_user: User = Depends(role_required([UserRole.ADMIN, UserRole.APPROVER, UserRole.MANAGER])),
    db: Session = Depends(get_db)
):
    """Enhanced document upload with AI extraction and duplicate detection"""
    
    if document_type not in ["invoice", "credit_note"]:
        raise HTTPException(400, "Document type must be 'invoice' or 'credit_note'")
    
    content = await file.read()
    validate_file(content, file.filename)
    
    # Save file
    safe_name = generate_secure_filename(file.filename)
    file_path = os.path.join(Config.UPLOAD_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(content)
    
    # Initialize AI extractor
    ai_extractor = AdvancedAIExtractor(
        use_openai=use_ai_enhancement,
        openai_api_key=os.getenv("OPENAI_API_KEY")
    )
    
    # Extract data based on file type
    ext = os.path.splitext(file.filename)[1].lower()
    if ext == ".pdf":
        extracted_data = await ai_extractor.extract_from_pdf(file_path)
    else:
        extracted_data = await ai_extractor.extract_from_image(file_path)
    
    # Advanced duplicate detection
    dup_detector = AdvancedDuplicateDetector()
    is_dup, dup_reason, similarity_scores = await dup_detector.check_duplicate_advanced(
        db, extracted_data, content, document_type
    )
    
    # Create document record
    doc = Document(
        filename=file.filename,
        file_path=file_path,
        file_hash=hashlib.sha256(content).hexdigest(),
        document_type=document_type,
        vendor_name=extracted_data.vendor_name,
        invoice_number=extracted_data.invoice_number,
        invoice_date=extracted_data.invoice_date,
        amount=extracted_data.amount,
        vat_amount=extracted_data.vat_amount,
        tax_rate=extracted_data.tax_rate,
        uploaded_by=current_user.id,
        status=ApprovalStatus.PENDING_LEVEL1,
        is_duplicate=is_dup,
        duplicate_reason=dup_reason
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    
    # Find similar documents (if not duplicate)
    similar_docs = []
    if not is_dup:
        similar_docs = await dup_detector.find_similar_documents(db, doc.id, limit=3)
    
    # Log audit
    audit = AuditLog(
        user_id=current_user.id,
        action="UPLOAD_ENHANCED",
        details=f"Uploaded {file.filename} (ID: {doc.id}) - AI extracted: {len([v for v in extracted_data.__dict__.values() if v])} fields",
        ip_address=request.client.host if hasattr(request, 'client') else "unknown"
    )
    db.add(audit)
    db.commit()
    
    return {
        "message": "Document uploaded successfully",
        "document_id": doc.id,
        "extracted_data": {
            "vendor_name": extracted_data.vendor_name,
            "invoice_number": extracted_data.invoice_number,
            "invoice_date": extracted_data.invoice_date,
            "due_date": extracted_data.due_date,
            "amount": extracted_data.amount,
            "vat_amount": extracted_data.vat_amount,
            "tax_rate": extracted_data.tax_rate,
            "po_number": extracted_data.po_number,
            "line_items_count": len(extracted_data.line_items),
            "confidence_scores": extracted_data.confidence_scores
        },
        "is_duplicate": is_dup,
        "duplicate_reason": dup_reason,
        "similarity_scores": similarity_scores,
        "similar_documents": [
            {
                "id": s["document_id"],
                "similarity": s["similarity"],
                "reason": s["reason"]
            }
            for s in similar_docs
        ],
        "status": doc.status.value
    }

# ==================== Add New AI Endpoints ====================

@app.post("/api/documents/{document_id}/re-extract")
async def re_extract_document(
    document_id: int,
    use_ai: bool = True,
    current_user: User = Depends(role_required([UserRole.ADMIN, UserRole.MANAGER])),
    db: Session = Depends(get_db)
):
    """Re-extract data from document using AI"""
    
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    
    # Initialize AI extractor
    ai_extractor = AdvancedAIExtractor(
        use_openai=use_ai,
        openai_api_key=os.getenv("OPENAI_API_KEY")
    )
    
    # Re-extract
    ext = os.path.splitext(doc.filename)[1].lower()
    if ext == ".pdf":
        extracted = await ai_extractor.extract_from_pdf(doc.file_path)
    else:
        extracted = await ai_extractor.extract_from_image(doc.file_path)
    
    # Update document with new extraction
    doc.vendor_name = extracted.vendor_name or doc.vendor_name
    doc.invoice_number = extracted.invoice_number or doc.invoice_number
    doc.invoice_date = extracted.invoice_date or doc.invoice_date
    doc.amount = extracted.amount or doc.amount
    doc.vat_amount = extracted.vat_amount or doc.vat_amount
    doc.tax_rate = extracted.tax_rate or doc.tax_rate
    
    db.commit()
    
    return {
        "message": "Document re-extracted successfully",
        "document_id": doc.id,
        "extracted_data": {
            "vendor_name": extracted.vendor_name,
            "invoice_number": extracted.invoice_number,
            "invoice_date": extracted.invoice_date,
            "due_date": extracted.due_date,
            "amount": extracted.amount,
            "vat_amount": extracted.vat_amount,
            "tax_rate": extracted.tax_rate,
            "confidence_scores": extracted.confidence_scores
        }
    }

@app.get("/api/documents/{document_id}/similar")
async def get_similar_documents(
    document_id: int,
    limit: int = 5,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Find documents similar to the given document"""
    
    dup_detector = AdvancedDuplicateDetector()
    similar = await dup_detector.find_similar_documents(db, document_id, limit)
    
    return {
        "document_id": document_id,
        "similar_documents": [
            {
                "id": s["document_id"],
                "similarity": s["similarity"],
                "reason": s["reason"],
                "vendor_name": s["document"].vendor_name,
                "amount": s["document"].amount,
                "date": s["document"].invoice_date
            }
            for s in similar
        ]
    }
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
    
    # Log audit
    audit = AuditLog(
        user_id=user.id, 
        action="LOGIN", 
        details="User logged in", 
        ip_address=request.client.host if hasattr(request, 'client') else "unknown"
    )
    db.add(audit)
    db.commit()
    return {
    "access_token": token, "token_type": "bearer"
}

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
        duplicate_reason=dup_reason
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    
    # Log audit
    audit = AuditLog(
        user_id=current_user.id, 
        action="UPLOAD", 
        details=f"Uploaded {file.filename} (ID: {doc.id})", 
        ip_address=request.client.host if hasattr(request, 'client') else "unknown"
    )
    db.add(audit)
    db.commit()
    
    return {
        "message": "Document uploaded",
        "document_id": doc.id,
        "extracted_data": extracted,
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
            "status": d.status.value,
            "upload_date": d.upload_date,
            "is_duplicate": d.is_duplicate
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
            "duplicate_reason": doc.duplicate_reason
        },
        "approval_history": [
            {
                "level": a.approval_level,
                "decision": a.decision,
                "comments": a.comments,
                "approved_at": a.approved_at
            }
            for a in approvals
        ]
    }

# ==================== Approval Routes (3-step) ====================
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
    
    allowed_level = None
    if current_user.role == UserRole.APPROVER and doc.status == ApprovalStatus.PENDING_LEVEL1:
        allowed_level = 1
    elif current_user.role == UserRole.MANAGER and doc.status == ApprovalStatus.PENDING_LEVEL2:
        allowed_level = 2
    elif current_user.role == UserRole.ADMIN and doc.status == ApprovalStatus.PENDING_LEVEL3:
        allowed_level = 3
    else:
        raise HTTPException(403, "Not authorized for this approval stage")
    
    if action.decision not in ["approved", "rejected"]:
        raise HTTPException(400, "Decision must be 'approved' or 'rejected'")
    
    approval = Approval(
        document_id=doc.id,
        approver_id=current_user.id,
        approval_level=allowed_level,
        decision=action.decision,
        comments=action.comments
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
    
    # Log audit
    audit = AuditLog(
        user_id=current_user.id, 
        action="APPROVAL", 
        details=f"Document {doc.id} {action.decision} at level {allowed_level}", 
        ip_address=request.client.host if hasattr(request, 'client') else "unknown"
    )
    db.add(audit)
    db.commit()
    
    return {
        "message": message,
        "document_id": doc.id,
        "status": doc.status.value,
        "approval_level": allowed_level
    }

@app.get("/api/approval/pending")
async def get_pending_approvals(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
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
            "vendor_name": d.vendor_name,
            "amount": d.amount,
            "status": d.status.value,
            "upload_date": d.upload_date
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
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)

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
    for d in docs:
        if d.vendor_name:
            vendor_breakdown[d.vendor_name] = vendor_breakdown.get(d.vendor_name, 0) + (d.amount or 0)
    monthly_trend = {}
    for d in docs:
        if d.invoice_date:
            month_key = d.invoice_date.strftime("%Y-%m")
            monthly_trend[month_key] = monthly_trend.get(month_key, 0) + (d.amount or 0)

    if current_user.role == UserRole.VIEWER:
        all_accessible = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED).all()
    else:
        all_accessible = db.query(Document).all()
    status_counts = {s.value: 0 for s in ApprovalStatus}
    for d in all_accessible:
        status_counts[d.status.value] += 1

    return {
        "summary": {
            "total_amount": total_amount,
            "total_vat": total_vat,
            "total_without_vat": total_amount - total_vat,
            "document_count": len(docs),
            "unique_vendors": len(vendor_breakdown)
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
                "vat": d.vat_amount
            }
            for d in docs[:50]
        ]
    }

@app.get("/api/reports/tax")
async def tax_report(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)
    
    if start_date:
        query = query.filter(Document.invoice_date >= start_date)
    if end_date:
        query = query.filter(Document.invoice_date <= end_date)
    
    docs = query.all()
    total_taxable = sum(d.amount or 0 for d in docs)
    total_vat = sum(d.vat_amount or 0 for d in docs)
    vendor_tax = {}
    for d in docs:
        if d.vendor_name:
            vendor_tax[d.vendor_name] = vendor_tax.get(d.vendor_name, 0) + (d.vat_amount or 0)
    
    return {
        "period": {"start_date": start_date, "end_date": end_date},
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
                "vat_amount": d.vat_amount
            }
            for d in docs[:50]
        ]
    }

# Export endpoints
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
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)
    
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
            "Upload Date": d.upload_date
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
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)
    
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

    docs = query.limit(100).all()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []
    title = Paragraph(f"Document Management Report - {datetime.now().strftime('%Y-%m-%d')}", styles['Title'])
    elements.append(title)
    elements.append(Paragraph("<br/><br/>", styles['Normal']))
    
    total_amount = sum(d.amount or 0 for d in docs)
    elements.append(Paragraph(f"<b>Total Amount:</b> ${total_amount:,.2f}", styles['Normal']))
    elements.append(Paragraph(f"<b>Number of Documents:</b> {len(docs)}", styles['Normal']))
    elements.append(Paragraph("<br/>", styles['Normal']))
    
    table_data = [["ID", "Vendor", "Invoice #", "Date", "Amount", "VAT"]]
    for d in docs:
        table_data.append([
            str(d.id),
            (d.vendor_name or "")[:30],
            (d.invoice_number or "")[:20],
            d.invoice_date.strftime("%Y-%m-%d") if d.invoice_date else "",
            f"${d.amount:,.2f}" if d.amount else "$0.00",
            f"${d.vat_amount:,.2f}" if d.vat_amount else "$0.00"
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

# Tax export endpoints
@app.get("/api/reports/export/tax-excel")
async def export_tax_excel(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)
    
    if start_date:
        query = query.filter(Document.invoice_date >= start_date)
    if end_date:
        query = query.filter(Document.invoice_date <= end_date)
    
    docs = query.all()
    data = []
    for d in docs:
        data.append({
            "Document ID": d.id,
            "Vendor": d.vendor_name or "",
            "Invoice Number": d.invoice_number or "",
            "Invoice Date": d.invoice_date,
            "Taxable Amount": d.amount or 0,
            "VAT Amount": d.vat_amount or 0,
            "Status": d.status.value
        })
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Tax Report', index=False)
        if data:
            summary = pd.DataFrame([
                ["Total Taxable Amount", df["Taxable Amount"].sum()],
                ["Total VAT Collected", df["VAT Amount"].sum()],
                ["Number of Transactions", len(df)],
                ["Effective Tax Rate", f"{(df['VAT Amount'].sum() / df['Taxable Amount'].sum() * 100) if df['Taxable Amount'].sum() > 0 else 0:.2f}%"]
            ], columns=["Metric", "Value"])
            summary.to_excel(writer, sheet_name='Summary', index=False)
    
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=tax_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"}
    )

@app.get("/api/reports/export/tax-pdf")
async def export_tax_pdf(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role == UserRole.VIEWER:
        query = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED)
    else:
        query = db.query(Document)
    
    if start_date:
        query = query.filter(Document.invoice_date >= start_date)
    if end_date:
        query = query.filter(Document.invoice_date <= end_date)
    
    docs = query.limit(100).all()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []
    title = Paragraph(f"Tax / VAT Report - {datetime.now().strftime('%Y-%m-%d')}", styles['Title'])
    elements.append(title)
    elements.append(Paragraph("<br/><br/>", styles['Normal']))
    
    total_taxable = sum(d.amount or 0 for d in docs)
    total_vat = sum(d.vat_amount or 0 for d in docs)
    elements.append(Paragraph(f"<b>Total Taxable Amount:</b> ${total_taxable:,.2f}", styles['Normal']))
    elements.append(Paragraph(f"<b>Total VAT Collected:</b> ${total_vat:,.2f}", styles['Normal']))
    elements.append(Paragraph(f"<b>Effective Tax Rate:</b> {(total_vat / total_taxable * 100) if total_taxable > 0 else 0:.2f}%", styles['Normal']))
    elements.append(Paragraph(f"<b>Number of Transactions:</b> {len(docs)}", styles['Normal']))
    elements.append(Paragraph("<br/>", styles['Normal']))
    
    table_data = [["ID", "Vendor", "Invoice #", "Date", "Taxable Amount", "VAT Amount"]]
    for d in docs:
        table_data.append([
            str(d.id),
            (d.vendor_name or "")[:30],
            (d.invoice_number or "")[:20],
            d.invoice_date.strftime("%Y-%m-%d") if d.invoice_date else "",
            f"${d.amount:,.2f}" if d.amount else "$0.00",
            f"${d.vat_amount:,.2f}" if d.vat_amount else "$0.00"
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
        headers={"Content-Disposition": f"attachment; filename=tax_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"}
    )

# ==================== Analytics Routes ====================
@app.get("/api/analytics/insights")
async def get_ai_insights(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
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
    
    insights = []
    monthly_values = list(monthly_spending.values())
    if len(monthly_values) >= 2:
        change = ((monthly_values[-1] - monthly_values[-2]) / monthly_values[-2] * 100) if monthly_values[-2] > 0 else 0
        if change > 0:
            insights.append(f"📈 Spending increased by {change:.1f}% compared to last month")
        elif change < 0:
            insights.append(f"📉 Spending decreased by {abs(change):.1f}% compared to last month")
    
    top_vendors = sorted(vendor_spending.items(), key=lambda x: x[1], reverse=True)[:3]
    if top_vendors:
        vendors_text = ", ".join([f"{v} (${a:,.0f})" for v, a in top_vendors])
        insights.append(f"🏢 Top 3 vendors: {vendors_text}")
    
    avg_amount = np.mean(amounts) if amounts else 0
    insights.append(f"💰 Average transaction amount: ${avg_amount:,.2f}")
    
    if monthly_spending:
        highest_month = max(monthly_spending, key=monthly_spending.get)
        insights.append(f"📅 Highest spending month: {highest_month} (${monthly_spending[highest_month]:,.2f})")
    
    total_vat = sum(d.vat_amount or 0 for d in docs)
    if total_vat > 0:
        insights.append(f"🧾 Total VAT collected: ${total_vat:,.2f}")
    
    return {
        "insights": insights,
        "anomalies": anomalies[:10],
        "statistics": {
            "total_spending": sum(amounts),
            "average_transaction": np.mean(amounts) if amounts else 0,
            "median_transaction": np.median(amounts) if amounts else 0,
            "total_transactions": len(docs),
            "unique_vendors": len(vendor_spending),
            "total_vat": total_vat
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
                "lower": round(forecast - confidence_interval, 2),
                "upper": round(forecast + confidence_interval, 2)
            },
            "trend": trend,
            "data_points": len(monthly_values),
            "historical_average": round(np.mean(monthly_values), 2),
            "recommendation": "Budget accordingly for next month based on the forecast" if forecast > np.mean(monthly_values) else "Expected spending to remain stable or decrease"
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

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc)}

# Mount static files (make sure static directory exists)
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
    print(f"🌐 Server will run on: http://0.0.0.0:{port}")
    print(f"📚 API Documentation: http://0.0.0.0:{port}/docs")
    print(f"🗄️  Database: {Config.DATABASE_TYPE.upper()}")
    print("=" * 60)
    
    uvicorn.run(
        "app:app", 
        host="0.0.0.0", 
        port=port, 
        reload=False
    )
