import os
import re
import io
import uuid
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum
from contextlib import asynccontextmanager
from collections import Counter

# FastAPI and dependencies
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, status, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Enum as SQLEnum, ForeignKey, Boolean, Text, and_, func, or_, desc
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
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

# Configuration
from dotenv import load_dotenv
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# ==================== Configuration ====================
class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this-in-production")
    if not SECRET_KEY or SECRET_KEY == "your-secret-key-change-this-in-production":
        logger.warning("⚠️ WARNING: Using default SECRET_KEY. Set a secure key in .env file!")

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
    
    # Render.com PostgreSQL URL (automatically provided)
    RENDER_DATABASE_URL = os.getenv("DATABASE_URL")
    
    SQLITE_URL = "sqlite:///./doc_management.db"

    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 10 * 1024 * 1024))
    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

    # OCR Configuration
    TESSERACT_CMD = os.getenv("TESSERACT_CMD", "tesseract")
    PDF_DPI = int(os.getenv("PDF_DPI", 300))

    @classmethod
    def get_database_url(cls):
        if cls.RENDER_DATABASE_URL:
            logger.info("✅ Using Render.com PostgreSQL database")
            database_url = cls.RENDER_DATABASE_URL
            if database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql://", 1)
            return database_url
        
        if cls.DATABASE_TYPE == "postgresql":
            logger.info(f"✅ Using PostgreSQL database at {cls.PG_HOST}:{cls.PG_PORT}")
            return f"postgresql://{cls.PG_USER}:{cls.PG_PASSWORD}@{cls.PG_HOST}:{cls.PG_PORT}/{cls.PG_DB}"
        elif cls.DATABASE_TYPE == "mysql":
            logger.info(f"✅ Using MySQL database at {cls.MYSQL_HOST}:{cls.MYSQL_PORT}")
            return f"mysql+pymysql://{cls.MYSQL_USER}:{cls.MYSQL_PASSWORD}@{cls.MYSQL_HOST}:{cls.MYSQL_PORT}/{cls.MYSQL_DB}"
        else:
            logger.warning("⚠️ Using SQLite database (development only)")
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

class DocumentType(str, Enum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    RECEIPT = "receipt"

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(SQLEnum(UserRole), nullable=False)
    full_name = Column(String(100))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    documents = relationship("Document", back_populates="uploader")
    approvals = relationship("Approval", back_populates="approver")
    audit_logs = relationship("AuditLog", back_populates="user")

class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_hash = Column(String(64), unique=True, index=True)
    document_type = Column(SQLEnum(DocumentType), nullable=False)
    vendor_name = Column(String(200), index=True)
    invoice_number = Column(String(100), index=True)
    invoice_date = Column(DateTime, index=True)
    amount = Column(Float, index=True)
    vat_amount = Column(Float)
    tax_rate = Column(Float)
    confidence_score = Column(Float, default=0.0)
    extraction_raw_data = Column(Text)  # JSON string of raw extracted data
    upload_date = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    status = Column(SQLEnum(ApprovalStatus), default=ApprovalStatus.PENDING_LEVEL1, index=True)
    is_duplicate = Column(Boolean, default=False, index=True)
    duplicate_reason = Column(Text)
    validation_errors = Column(Text)  # JSON string of validation issues
    
    # Relationships
    uploader = relationship("User", back_populates="documents")
    approvals = relationship("Approval", back_populates="document")
    
    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "document_type": self.document_type.value if self.document_type else None,
            "vendor_name": self.vendor_name,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "amount": self.amount,
            "vat_amount": self.vat_amount,
            "confidence_score": self.confidence_score,
            "status": self.status.value if self.status else None,
            "upload_date": self.upload_date.isoformat() if self.upload_date else None,
            "is_duplicate": self.is_duplicate,
        }

class Approval(Base):
    __tablename__ = "approvals"
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    approver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    approval_level = Column(Integer, nullable=False)
    decision = Column(String(20), nullable=False)
    comments = Column(Text)
    approved_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    document = relationship("Document", back_populates="approvals")
    approver = relationship("User", back_populates="approvals")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String(100), nullable=False, index=True)
    details = Column(Text)
    ip_address = Column(String(45))
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    
    # Relationships
    user = relationship("User", back_populates="audit_logs")

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
    elif "mysql" in database_url:
        engine = create_engine(
            database_url, 
            pool_pre_ping=True,
            pool_recycle=3600
        )
    else:
        engine = create_engine(
            database_url, 
            connect_args={"check_same_thread": False}
        )
    
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    logger.info("✅ Database engine created successfully")
    
except Exception as e:
    logger.error(f"❌ Database connection error: {e}")
    logger.warning("⚠️ Falling back to SQLite...")
    Config.DATABASE_TYPE = "sqlite"
    engine = create_engine(
        Config.SQLITE_URL, 
        connect_args={"check_same_thread": False}
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    logger.info("✅ SQLite fallback engine created")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==================== Pydantic Schemas ====================
class UserLogin(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6)
    full_name: str = Field(..., min_length=1)
    role: UserRole
    
    @field_validator('username')
    @classmethod
    def username_alphanumeric(cls, v):
        if not re.match(r'^[a-zA-Z0-9_]+$', v):
            raise ValueError('Username must be alphanumeric with underscores')
        return v

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class ApprovalAction(BaseModel):
    document_id: int
    decision: str = Field(..., pattern="^(approved|rejected)$")
    comments: Optional[str] = Field(None, max_length=500)

class ReportFilter(BaseModel):
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    vendor_name: Optional[str] = None
    status: Optional[str] = None
    min_amount: Optional[float] = Field(None, ge=0)
    max_amount: Optional[float] = Field(None, ge=0)

class DocumentResponse(BaseModel):
    id: int
    filename: str
    document_type: str
    vendor_name: Optional[str]
    invoice_number: Optional[str]
    amount: Optional[float]
    vat_amount: Optional[float]
    confidence_score: float
    status: str
    upload_date: datetime
    is_duplicate: bool

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
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User inactive or not found")
    return user

def role_required(required_roles: List[UserRole]):
    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in required_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return checker

# ==================== OCR and Text Extraction ====================
def extract_text_ocr(image_path: str) -> str:
    """Extract text from image using Tesseract OCR"""
    try:
        pytesseract.pytesseract.tesseract_cmd = Config.TESSERACT_CMD
        image = Image.open(image_path)
        # Preprocess image for better OCR
        image = image.convert('L')  # Convert to grayscale
        text = pytesseract.image_to_string(image)
        return text.strip()
    except Exception as e:
        logger.error(f"OCR error: {e}")
        return ""

def extract_pdf_text(pdf_path: str) -> str:
    """Extract text from PDF using pdf2image and OCR"""
    try:
        images = pdf2image.convert_from_path(pdf_path, dpi=Config.PDF_DPI)
        all_text = []
        for image in images:
            image = image.convert('L')
            text = pytesseract.image_to_string(image)
            all_text.append(text)
        return "\n".join(all_text).strip()
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return ""

# ==================== AI Document Extractor ====================
class AIExtractor:
    """AI-powered document field extraction with confidence scoring"""
    
    # Enhanced regex patterns for field extraction
    PATTERNS = {
        "vendor_name": [
            r"(?:Vendor|Supplier|Company|Bill From|Seller|Issuer|From)[\s:]+([A-Za-z0-9\s&.,]+)(?:\n|$)",
            r"(?:Sold by|Prepared by)[\s:]+([A-Za-z0-9\s&.,]+)(?:\n|$)",
            r"^([A-Za-z0-9\s&.,]+)(?:\n|$)"
        ],
        "invoice_number": [
            r"(?:Invoice|Document|Bill)[\s#:]+(\S+)",
            r"(?:INV|INVOICE)[\s-]*(\d+[A-Za-z0-9\-]*)",
            r"Number[\s:]+(\S+)"
        ],
        "invoice_date": [
            r"(?:Date|Invoice Date|Issue Date|Date of Issue)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"(\d{4}-\d{2}-\d{2})",
            r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})"
        ],
        "amount": [
            r"(?:Total|Amount Due|Grand Total|Balance Due|Invoice Total)[\s:]*[$€£]?([\d,]+\.?\d*)",
            r"TOTAL\s+[$€£]?([\d,]+\.?\d*)",
            r"Amount[\s:]+[$€£]?([\d,]+\.?\d*)"
        ],
        "vat_amount": [
            r"(?:VAT|Tax|GST|HST|VAT Amount|Tax Amount)[\s:]*[$€£]?([\d,]+\.?\d*)",
            r"Tax\s+[$€£]?([\d,]+\.?\d*)"
        ],
        "tax_rate": [
            r"(?:VAT Rate|Tax Rate)[\s:]+(\d+(?:\.\d+)?)%",
            r"Rate[\s:]+(\d+(?:\.\d+)?)%"
        ]
    }
    
    DATE_FORMATS = [
        "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d",
        "%m-%d-%Y", "%d-%m-%Y", "%b %d, %Y",
        "%B %d, %Y", "%d %b %Y", "%d %B %Y"
    ]
    
    @classmethod
    async def extract_from_image(cls, image_path: str) -> Dict[str, Any]:
        """Extract fields from image with OCR"""
        text = extract_text_ocr(image_path)
        return cls._extract_fields(text)
    
    @classmethod
    async def extract_from_pdf(cls, pdf_path: str) -> Dict[str, Any]:
        """Extract fields from PDF with OCR"""
        text = extract_pdf_text(pdf_path)
        return cls._extract_fields(text)
    
    @classmethod
    def _extract_fields(cls, text: str) -> Dict[str, Any]:
        """Extract structured fields from OCR text with confidence scoring"""
        data = {
            "vendor_name": None,
            "invoice_number": None,
            "invoice_date": None,
            "amount": None,
            "vat_amount": None,
            "tax_rate": None
        }
        
        confidence_scores = {}
        
        for key, patterns in cls.PATTERNS.items():
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
                if match:
                    value = match.group(1).strip()
                    if key == "invoice_date":
                        parsed_date = cls._parse_date(value)
                        if parsed_date:
                            data[key] = parsed_date
                            confidence_scores[key] = 0.9
                            break
                    elif key in ("amount", "vat_amount", "tax_rate"):
                        parsed_value = cls._parse_number(value)
                        if parsed_value is not None:
                            data[key] = parsed_value
                            confidence_scores[key] = 0.85
                            break
                    else:
                        data[key] = value
                        confidence_scores[key] = 0.8
                        break
        
        # Calculate overall confidence score
        data["confidence_score"] = cls._calculate_confidence(data, confidence_scores)
        data["raw_text"] = text[:500]  # Store preview of raw text
        
        return data
    
    @classmethod
    def _parse_date(cls, date_str: str) -> Optional[datetime]:
        """Parse date string with multiple formats"""
        date_str = date_str.strip()
        for fmt in cls.DATE_FORMATS:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None
    
    @classmethod
    def _parse_number(cls, num_str: str) -> Optional[float]:
        """Parse currency/number string to float"""
        try:
            # Remove currency symbols and commas
            cleaned = re.sub(r'[^\d\-.,]', '', num_str)
            # Handle European decimal format (1.234,56)
            if ',' in cleaned and '.' in cleaned:
                cleaned = cleaned.replace('.', '').replace(',', '.')
            elif ',' in cleaned and cleaned.count(',') == 1:
                cleaned = cleaned.replace(',', '.')
            return float(cleaned)
        except (ValueError, TypeError):
            return None
    
    @classmethod
    def _calculate_confidence(cls, data: Dict, confidence_scores: Dict) -> float:
        """Calculate overall confidence score based on extracted fields"""
        weights = {
            "vendor_name": 0.15,
            "invoice_number": 0.25,
            "invoice_date": 0.2,
            "amount": 0.3,
            "vat_amount": 0.1
        }
        
        total_weight = 0
        weighted_sum = 0
        
        for field, weight in weights.items():
            if data.get(field) is not None:
                weighted_sum += weight * confidence_scores.get(field, 0.5)
                total_weight += weight
        
        if total_weight == 0:
            return 0.0
        
        return round(weighted_sum / total_weight, 2)

# ==================== Validation Module ====================
class DataValidator:
    """Validate extracted data for consistency and business rules"""
    
    @staticmethod
    def validate(data: Dict[str, Any]) -> Tuple[bool, List[str], Dict[str, Any]]:
        """
        Validate extracted data.
        Returns: (is_valid, error_messages, corrected_data)
        """
        errors = []
        corrected = data.copy()
        
        # Validate amount
        if data.get("amount") is not None:
            if data["amount"] < 0:
                errors.append("Amount cannot be negative")
                corrected["amount"] = None
            elif data["amount"] > 1_000_000:
                errors.append("Amount exceeds maximum allowed ($1,000,000)")
                # Don't nullify, just warn
        
        # Validate VAT amount
        if data.get("vat_amount") is not None:
            if data["vat_amount"] < 0:
                errors.append("VAT amount cannot be negative")
                corrected["vat_amount"] = None
            elif data.get("amount") and data["vat_amount"] > data["amount"]:
                errors.append("VAT amount exceeds total amount")
                corrected["vat_amount"] = None
        
        # Validate tax rate
        if data.get("tax_rate") is not None:
            if data["tax_rate"] < 0 or data["tax_rate"] > 100:
                errors.append("Tax rate must be between 0 and 100")
                corrected["tax_rate"] = None
        
        # Validate invoice date
        if data.get("invoice_date") is not None:
            if data["invoice_date"] > datetime.now(timezone.utc):
                errors.append("Invoice date cannot be in the future")
                corrected["invoice_date"] = None
            elif data["invoice_date"] < datetime.now(timezone.utc) - timedelta(days=365*5):
                errors.append("Invoice date is more than 5 years old - please verify")
                # Just warn, don't nullify
        
        # Cross-validation: Calculate expected VAT if tax rate and amount available
        if (data.get("amount") and data.get("tax_rate") and 
            data.get("vat_amount") is not None):
            expected_vat = round(data["amount"] * data["tax_rate"] / 100, 2)
            if abs(data["vat_amount"] - expected_vat) > 0.01:
                errors.append(f"VAT amount ({data['vat_amount']}) doesn't match {data['tax_rate']}% of amount ({expected_vat})")
                # Don't auto-correct, flag for review
        
        is_valid = len(errors) == 0
        return is_valid, errors, corrected

# ==================== Duplicate Detection ====================
class DuplicateDetector:
    """Detect duplicate documents using multiple strategies"""
    
    @staticmethod
    def check_duplicate(
        db: Session, 
        invoice_number: Optional[str], 
        vendor_name: Optional[str],
        amount: Optional[float], 
        file_content: bytes, 
        document_type: DocumentType,
        invoice_date: Optional[datetime] = None
    ) -> Tuple[bool, Optional[str], Optional[Document]]:
        """
        Check for duplicate documents.
        Returns: (is_duplicate, reason, existing_document)
        """
        # 1. File hash duplicate (exact file match)
        file_hash = hashlib.sha256(file_content).hexdigest()
        existing_by_hash = db.query(Document).filter(Document.file_hash == file_hash).first()
        if existing_by_hash:
            return True, f"Duplicate file content detected (Document #{existing_by_hash.id})", existing_by_hash
        
        # 2. Invoice number match (for invoices only)
        if invoice_number and document_type == DocumentType.INVOICE:
            existing_by_inv = db.query(Document).filter(
                Document.invoice_number == invoice_number,
                Document.document_type == DocumentType.INVOICE
            ).first()
            if existing_by_inv:
                return True, f"Duplicate invoice number: {invoice_number} (Document #{existing_by_inv.id})", existing_by_inv
        
        # 3. Credit note referencing original invoice (allowed)
        if invoice_number and document_type == DocumentType.CREDIT_NOTE:
            original_invoice = db.query(Document).filter(
                Document.invoice_number == invoice_number,
                Document.document_type == DocumentType.INVOICE
            ).first()
            if original_invoice:
                return False, None, None  # Credit note is valid, not a duplicate
        
        # 4. Vendor + amount + date window check (for invoices)
        if document_type == DocumentType.INVOICE and vendor_name and amount and invoice_date:
            thirty_days_window = datetime.now(timezone.utc) - timedelta(days=30)
            potential_dup = db.query(Document).filter(
                and_(
                    Document.vendor_name == vendor_name,
                    Document.amount == amount,
                    func.abs(func.julianday(Document.invoice_date) - func.julianday(invoice_date)) <= 5,
                    Document.document_type == DocumentType.INVOICE
                )
            ).first()
            if potential_dup:
                return True, f"Possible duplicate: same vendor, amount, and close date found (Document #{potential_dup.id})", potential_dup
        
        return False, None, None

# ==================== Lifespan (Startup) ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting Document Management System...")
    logger.info(f"📊 Database Type: {Config.DATABASE_TYPE}")
    
    # Create tables
    if engine:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Database tables created/verified")
    
    # Create upload directory
    os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
    logger.info(f"✅ Upload directory: {Config.UPLOAD_DIR}")
    
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
        logger.info("✅ Default users created")
    except Exception as e:
        logger.error(f"⚠️ Startup error: {e}")
        db.rollback()
    finally:
        db.close()
    
    logger.info("=" * 60)
    logger.info("📄 Document Management System Ready!")
    logger.info("=" * 60)
    logger.info("Default Login Credentials:")
    logger.info("  👑 Admin:    admin / Admin@123")
    logger.info("  ✅ Approver: approver / Approver@123")
    logger.info("  📊 Manager:  manager / Manager@123")
    logger.info("  👁️ Viewer:   viewer / Viewer@123")
    logger.info("=" * 60)
    
    yield
    
    logger.info("🛑 Shutting down...")

# ==================== FastAPI App ====================
app = FastAPI(
    title="DocManager Pro", 
    version="3.0", 
    description="Document Management System with OCR, AI Extraction, Validation, and Approval Workflow",
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
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",
        max_age=Config.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    
    # Log audit
    audit = AuditLog(
        user_id=user.id, 
        action="LOGIN", 
        details=f"User {user.username} logged in", 
        ip_address=request.client.host if request.client else "unknown"
    )
    db.add(audit)
    db.commit()
    
    return {"access_token": token, "token_type": "bearer"}

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

# ==================== File Validation ====================
def validate_file(file_content: bytes, filename: str) -> None:
    """Validate uploaded file size and type"""
    if len(file_content) > Config.MAX_FILE_SIZE:
        raise HTTPException(400, f"Max size {Config.MAX_FILE_SIZE//1024//1024}MB")
    
    ext = os.path.splitext(filename)[1].lower()
    if ext not in Config.ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Allowed extensions: {Config.ALLOWED_EXTENSIONS}")
    
    # Magic number validation
    magic_map = {
        b'%PDF': '.pdf', 
        b'\xff\xd8': '.jpg', 
        b'\x89PNG': '.png'
    }
    detected = None
    for magic, ext2 in magic_map.items():
        if file_content.startswith(magic):
            detected = ext2
            break
    
    if detected and detected != ext:
        raise HTTPException(400, "File extension mismatch - file appears to be a different type")

def generate_secure_filename(original: str) -> str:
    """Generate secure random filename"""
    ext = os.path.splitext(original)[1].lower()
    return f"{uuid.uuid4().hex}{ext}"

# ==================== Document Routes ====================
@app.post("/api/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    document_type: DocumentType = Form(...),
    current_user: User = Depends(role_required([UserRole.ADMIN, UserRole.APPROVER, UserRole.MANAGER])),
    db: Session = Depends(get_db)
):
    """Upload and process document with OCR, AI extraction, validation, and duplicate detection"""
    
    content = await file.read()
    validate_file(content, file.filename)
    
    # Save file
    safe_name = generate_secure_filename(file.filename)
    file_path = os.path.join(Config.UPLOAD_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(content)
    
    file_hash = hashlib.sha256(content).hexdigest()
    ext = os.path.splitext(file.filename)[1].lower()
    
    # Extract data using AI
    if ext == ".pdf":
        extracted = await AIExtractor.extract_from_pdf(file_path)
    else:
        extracted = await AIExtractor.extract_from_image(file_path)
    
    # Validate extracted data
    is_valid, validation_errors, corrected_data = DataValidator.validate(extracted)
    
    # Check for duplicates
    is_dup, dup_reason, existing_doc = DuplicateDetector.check_duplicate(
        db, 
        corrected_data.get("invoice_number"), 
        corrected_data.get("vendor_name"),
        corrected_data.get("amount"), 
        content, 
        document_type,
        corrected_data.get("invoice_date")
    )
    
    # Create document record
    doc = Document(
        filename=file.filename,
        file_path=file_path,
        file_hash=file_hash,
        document_type=document_type,
        vendor_name=corrected_data.get("vendor_name"),
        invoice_number=corrected_data.get("invoice_number"),
        invoice_date=corrected_data.get("invoice_date"),
        amount=corrected_data.get("amount"),
        vat_amount=corrected_data.get("vat_amount"),
        tax_rate=corrected_data.get("tax_rate"),
        confidence_score=corrected_data.get("confidence_score", 0.0),
        extraction_raw_data=str({k: str(v) for k, v in extracted.items()}),
        uploaded_by=current_user.id,
        status=ApprovalStatus.PENDING_LEVEL1,
        is_duplicate=is_dup,
        duplicate_reason=dup_reason,
        validation_errors=str(validation_errors) if validation_errors else None
    )
    
    db.add(doc)
    db.commit()
    db.refresh(doc)
    
    # Log audit
    audit = AuditLog(
        user_id=current_user.id, 
        action="UPLOAD", 
        details=f"Uploaded {file.filename} (ID: {doc.id}), confidence: {doc.confidence_score}", 
        ip_address=request.client.host if request.client else "unknown"
    )
    db.add(audit)
    db.commit()
    
    response_data = {
        "message": "Document uploaded successfully" if not is_dup else "Document uploaded but flagged as duplicate",
        "document_id": doc.id,
        "extracted_data": {
            "vendor_name": corrected_data.get("vendor_name"),
            "invoice_number": corrected_data.get("invoice_number"),
            "invoice_date": corrected_data.get("invoice_date"),
            "amount": corrected_data.get("amount"),
            "vat_amount": corrected_data.get("vat_amount"),
            "tax_rate": corrected_data.get("tax_rate"),
            "confidence_score": doc.confidence_score
        },
        "validation": {
            "is_valid": is_valid,
            "errors": validation_errors
        },
        "is_duplicate": is_dup,
        "duplicate_reason": dup_reason,
        "status": doc.status.value
    }
    
    return response_data

@app.get("/api/documents", response_model=List[DocumentResponse])
async def list_documents(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    status_filter: Optional[ApprovalStatus] = None,
    document_type: Optional[DocumentType] = None,
    vendor_name: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List documents with filtering"""
    query = db.query(Document)
    
    # Role-based access control
    if current_user.role == UserRole.VIEWER:
        query = query.filter(Document.status == ApprovalStatus.APPROVED)
    
    # Apply filters
    if status_filter:
        query = query.filter(Document.status == status_filter)
    if document_type:
        query = query.filter(Document.document_type == document_type)
    if vendor_name:
        query = query.filter(Document.vendor_name.contains(vendor_name))
    if start_date:
        query = query.filter(Document.invoice_date >= start_date)
    if end_date:
        query = query.filter(Document.invoice_date <= end_date)
    
    docs = query.order_by(desc(Document.upload_date)).offset(skip).limit(limit).all()
    
    return [DocumentResponse(**doc.to_dict()) for doc in docs]

@app.get("/api/documents/{document_id}")
async def get_document(
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get detailed document information"""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    
    if current_user.role == UserRole.VIEWER and doc.status != ApprovalStatus.APPROVED:
        raise HTTPException(403, "Access denied - document not approved")
    
    approvals = db.query(Approval).filter(Approval.document_id == document_id).order_by(Approval.approval_level).all()
    
    return {
        "document": doc.to_dict(),
        "approval_history": [
            {
                "level": a.approval_level,
                "decision": a.decision,
                "comments": a.comments,
                "approved_at": a.approved_at.isoformat() if a.approved_at else None,
                "approver": db.query(User).filter(User.id == a.approver_id).first().username if a.approver_id else None
            }
            for a in approvals
        ],
        "validation_errors": doc.validation_errors,
        "duplicate_info": {
            "is_duplicate": doc.is_duplicate,
            "reason": doc.duplicate_reason
        } if doc.is_duplicate else None
    }

# ==================== Approval Routes (3-step) ====================
@app.post("/api/approval/process")
async def process_approval(
    action: ApprovalAction,
    request: Request,
    current_user: User = Depends(role_required([UserRole.ADMIN, UserRole.APPROVER, UserRole.MANAGER])),
    db: Session = Depends(get_db)
):
    """Process document approval through 3-step workflow"""
    doc = db.query(Document).filter(Document.id == action.document_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    
    if doc.status in [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]:
        raise HTTPException(400, f"Document already {doc.status.value}")
    
    # Determine allowed approval level based on user role and current status
    allowed_level = None
    if current_user.role == UserRole.APPROVER and doc.status == ApprovalStatus.PENDING_LEVEL1:
        allowed_level = 1
    elif current_user.role == UserRole.MANAGER and doc.status == ApprovalStatus.PENDING_LEVEL2:
        allowed_level = 2
    elif current_user.role == UserRole.ADMIN and doc.status == ApprovalStatus.PENDING_LEVEL3:
        allowed_level = 3
    else:
        raise HTTPException(403, "Not authorized for this approval stage")
    
    # Check if user already approved this document at this level
    existing = db.query(Approval).filter(
        Approval.document_id == doc.id,
        Approval.approval_level == allowed_level
    ).first()
    if existing:
        raise HTTPException(400, f"You have already processed this document at level {allowed_level}")
    
    # Create approval record
    approval = Approval(
        document_id=doc.id,
        approver_id=current_user.id,
        approval_level=allowed_level,
        decision=action.decision,
        comments=action.comments
    )
    db.add(approval)
    
    # Update document status
    if action.decision == "rejected":
        doc.status = ApprovalStatus.REJECTED
        message = f"Document #{doc.id} rejected at level {allowed_level} by {current_user.username}"
    else:  # approved
        if allowed_level == 1:
            doc.status = ApprovalStatus.PENDING_LEVEL2
            message = f"Document #{doc.id} approved at level 1, moved to level 2"
        elif allowed_level == 2:
            doc.status = ApprovalStatus.PENDING_LEVEL3
            message = f"Document #{doc.id} approved at level 2, moved to level 3"
        else:  # level 3 (admin)
            doc.status = ApprovalStatus.APPROVED
            message = f"Document #{doc.id} fully approved"
    
    db.commit()
    
    # Log audit
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
    """Get documents pending approval for the current user"""
    if current_user.role == UserRole.APPROVER:
        docs = db.query(Document).filter(Document.status == ApprovalStatus.PENDING_LEVEL1).all()
    elif current_user.role == UserRole.MANAGER:
        docs = db.query(Document).filter(Document.status == ApprovalStatus.PENDING_LEVEL2).all()
    elif current_user.role == UserRole.ADMIN:
        docs = db.query(Document).filter(Document.status == ApprovalStatus.PENDING_LEVEL3).all()
    else:
        docs = []
    
    # Filter out documents the user has already approved
    user_approved_ids = set()
    if docs:
        approvals = db.query(Approval).filter(
            Approval.approver_id == current_user.id,
            Approval.document_id.in_([d.id for d in docs])
        ).all()
        user_approved_ids = {a.document_id for a in approvals}
    
    pending_docs = [d for d in docs if d.id not in user_approved_ids]
    
    return [
        {
            "id": d.id,
            "filename": d.filename,
            "vendor_name": d.vendor_name,
            "amount": d.amount,
            "confidence_score": d.confidence_score,
            "status": d.status.value,
            "upload_date": d.upload_date
        }
        for d in pending_docs
    ]

# ==================== Report Routes ====================
@app.post("/api/reports/spend-summary")
async def spend_summary(
    filters: ReportFilter,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate spending summary report"""
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
    
    # Status overview (all accessible documents)
    if current_user.role == UserRole.VIEWER:
        all_accessible = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED).all()
    else:
        all_accessible = db.query(Document).all()
    
    status_counts = {s.value: 0 for s in ApprovalStatus}
    for d in all_accessible:
        status_counts[d.status.value] += 1
    
    return {
        "summary": {
            "total_amount": round(total_amount, 2),
            "total_vat": round(total_vat, 2),
            "total_without_vat": round(total_amount - total_vat, 2),
            "document_count": len(docs),
            "unique_vendors": len(vendor_breakdown),
            "average_amount": round(total_amount / len(docs), 2) if docs else 0
        },
        "vendor_breakdown": {k: round(v, 2) for k, v in sorted(vendor_breakdown.items(), key=lambda x: x[1], reverse=True)[:10]},
        "monthly_trend": {k: round(v, 2) for k, v in sorted(monthly_trend.items())},
        "status_overview": status_counts,
        "documents": [
            {
                "id": d.id,
                "vendor": d.vendor_name,
                "invoice_number": d.invoice_number,
                "date": d.invoice_date.isoformat() if d.invoice_date else None,
                "amount": d.amount,
                "vat": d.vat_amount,
                "confidence": d.confidence_score
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
    """Generate tax/VAT report"""
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
    
    # Monthly tax breakdown
    monthly_tax = {}
    for d in docs:
        if d.invoice_date and d.vat_amount:
            month_key = d.invoice_date.strftime("%Y-%m")
            monthly_tax[month_key] = monthly_tax.get(month_key, 0) + d.vat_amount
    
    return {
        "period": {"start_date": start_date.isoformat() if start_date else None, "end_date": end_date.isoformat() if end_date else None},
        "summary": {
            "total_taxable_amount": round(total_taxable, 2),
            "total_vat_collected": round(total_vat, 2),
            "effective_tax_rate": round((total_vat / total_taxable * 100), 2) if total_taxable > 0 else 0,
            "transaction_count": len(docs)
        },
        "vendor_tax_breakdown": {k: round(v, 2) for k, v in sorted(vendor_tax.items(), key=lambda x: x[1], reverse=True)[:10]},
        "monthly_tax": {k: round(v, 2) for k, v in sorted(monthly_tax.items())},
        "transactions": [
            {
                "id": d.id,
                "vendor": d.vendor_name,
                "invoice_number": d.invoice_number,
                "date": d.invoice_date.isoformat() if d.invoice_date else None,
                "taxable_amount": d.amount,
                "vat_amount": d.vat_amount,
                "tax_rate": d.tax_rate
            }
            for d in docs[:50]
        ]
    }

# ==================== Export Endpoints ====================
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
            "Type": d.document_type.value if d.document_type else "",
            "Vendor": d.vendor_name or "",
            "Invoice Number": d.invoice_number or "",
            "Date": d.invoice_date.strftime("%Y-%m-%d") if d.invoice_date else "",
            "Amount": d.amount or 0,
            "VAT Amount": d.vat_amount or 0,
            "Tax Rate (%)": d.tax_rate or 0,
            "Confidence Score": d.confidence_score,
            "Status": d.status.value,
            "Upload Date": d.upload_date.strftime("%Y-%m-%d %H:%M") if d.upload_date else "",
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
                ["Average Amount", df["Amount"].mean()],
                ["Average Confidence Score", df["Confidence Score"].mean()]
            ], columns=["Metric", "Value"])
            summary.to_excel(writer, sheet_name='Summary', index=False)
            
            # Status breakdown
            status_counts = df["Status"].value_counts().reset_index()
            status_counts.columns = ["Status", "Count"]
            status_counts.to_excel(writer, sheet_name='Status Breakdown', index=False)
    
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
        query = query.filter(Document.vendor_name.contains(vendor_name))
    if status:
        query = query.filter(Document.status == status)
    if min_amount is not None:
        query = query.filter(Document.amount >= min_amount)
    if max_amount is not None:
        query = query.filter(Document.amount <= max_amount)
    
    docs = query.limit(200).all()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    elements = []
    
    # Title
    title = Paragraph(f"Document Management Report - {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 0.2 * inch))
    
    # Summary
    total_amount = sum(d.amount or 0 for d in docs)
    total_vat = sum(d.vat_amount or 0 for d in docs)
    
    summary_data = [
        ["Metric", "Value"],
        ["Total Amount", f"${total_amount:,.2f}"],
        ["Total VAT", f"${total_vat:,.2f}"],
        ["Number of Documents", str(len(docs))],
        ["Average Amount", f"${total_amount/len(docs):,.2f}" if docs else "$0"],
        ["Average Confidence", f"{sum(d.confidence_score for d in docs)/len(docs)*100:.1f}%" if docs else "0%"]
    ]
    
    summary_table = Table(summary_data, colWidths=[2.5 * inch, 2.5 * inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.grey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 0.2 * inch))
    
    # Documents table
    table_data = [["ID", "Vendor", "Invoice #", "Date", "Amount", "VAT", "Confidence"]]
    for d in docs[:100]:
        table_data.append([
            str(d.id),
            (d.vendor_name or "")[:30],
            (d.invoice_number or "")[:20],
            d.invoice_date.strftime("%Y-%m-%d") if d.invoice_date else "",
            f"${d.amount:,.2f}" if d.amount else "$0.00",
            f"${d.vat_amount:,.2f}" if d.vat_amount else "$0.00",
            f"{d.confidence_score*100:.0f}%"
        ])
    
    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.grey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 9),
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
    """Generate AI-powered insights and anomaly detection"""
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
    
    # Anomaly detection using Z-score
    anomalies = []
    if len(amounts) > 1:
        mean_amt = np.mean(amounts)
        std_amt = np.std(amounts)
        if std_amt > 0:
            for d in docs:
                if d.amount and abs(d.amount - mean_amt) > 2 * std_amt:
                    anomalies.append({
                        "document_id": d.id,
                        "vendor": d.vendor_name,
                        "amount": d.amount,
                        "date": d.invoice_date.isoformat() if d.invoice_date else None,
                        "z_score": round((d.amount - mean_amt) / std_amt, 2),
                        "reason": f"Amount is {((d.amount - mean_amt) / std_amt):.1f} standard deviations from mean"
                    })
    
    # Generate insights
    insights = []
    monthly_values = list(monthly_spending.values())
    if len(monthly_values) >= 2:
        change = ((monthly_values[-1] - monthly_values[-2]) / monthly_values[-2] * 100) if monthly_values[-2] > 0 else 0
        if change > 10:
            insights.append(f"📈 Spending increased significantly by {change:.1f}% compared to last month")
        elif change < -10:
            insights.append(f"📉 Spending decreased significantly by {abs(change):.1f}% compared to last month")
        elif change != 0:
            insights.append(f"📊 Spending changed by {change:.1f}% compared to last month")
    
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
    
    # Confidence score analysis
    low_confidence_docs = [d for d in docs if d.confidence_score < 0.6]
    if low_confidence_docs:
        insights.append(f"⚠️ {len(low_confidence_docs)} documents have low extraction confidence (<60%) - manual review recommended")
    
    return {
        "insights": insights,
        "anomalies": anomalies[:10],
        "statistics": {
            "total_spending": round(sum(amounts), 2),
            "average_transaction": round(np.mean(amounts), 2) if amounts else 0,
            "median_transaction": round(np.median(amounts), 2) if amounts else 0,
            "total_transactions": len(docs),
            "unique_vendors": len(vendor_spending),
            "total_vat": round(total_vat, 2),
            "average_confidence": round(np.mean([d.confidence_score for d in docs]), 2) if docs else 0
        },
        "trends": {
            "monthly_spending": {k: round(v, 2) for k, v in sorted(monthly_spending.items())[-12:]},
            "top_5_vendors": {k: round(v, 2) for k, v in sorted(vendor_spending.items(), key=lambda x: x[1], reverse=True)[:5]}
        }
    }

@app.get("/api/analytics/forecast")
async def get_spending_forecast(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate spending forecast based on historical data"""
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
        # Simple moving average forecast
        forecast = np.mean(monthly_values[-3:])
        confidence_interval = np.std(monthly_values[-3:]) * 1.96  # 95% confidence interval
        
        # Trend analysis
        if len(monthly_values) >= 2:
            if monthly_values[-1] > monthly_values[-2] * 1.1:
                trend = "increasing"
                recommendation = "Budget increase recommended for next month"
            elif monthly_values[-1] < monthly_values[-2] * 0.9:
                trend = "decreasing"
                recommendation = "Expected spending to decrease - consider reallocating budget"
            else:
                trend = "stable"
                recommendation = "Spending expected to remain stable"
        else:
            trend = "stable"
            recommendation = "Insufficient trend data for specific recommendation"
        
        return {
            "forecast_next_month": round(forecast, 2),
            "confidence_interval": {
                "lower": round(max(0, forecast - confidence_interval), 2),
                "upper": round(forecast + confidence_interval, 2)
            },
            "trend": trend,
            "data_points": len(monthly_values),
            "historical_average": round(np.mean(monthly_values), 2),
            "recommendation": recommendation,
            "monthly_trend": {k: round(v, 2) for k, v in sorted(monthly_totals.items())[-6:]}
        }
    else:
        return {"message": f"Need at least 3 months of data for forecast. Currently have {len(monthly_values)} months."}

# ==================== Admin Routes ====================
@app.get("/api/admin/users")
async def list_users(
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    """List all users (admin only)"""
    users = db.query(User).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "full_name": u.full_name,
            "role": u.role.value,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None
        }
        for u in users
    ]

@app.post("/api/admin/users")
async def create_user(
    user_data: UserCreate,
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    """Create new user (admin only)"""
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

@app.put("/api/admin/users/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: int,
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    """Toggle user active status (admin only)"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == current_user.id:
        raise HTTPException(400, "Cannot deactivate your own account")
    
    user.is_active = not user.is_active
    db.commit()
    
    return {"message": f"User {user.username} {'activated' if user.is_active else 'deactivated'}"}

@app.get("/api/admin/audit-logs")
async def get_audit_logs(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    action: Optional[str] = None,
    user_id: Optional[int] = None,
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    """Get audit logs (admin only)"""
    query = db.query(AuditLog)
    
    if action:
        query = query.filter(AuditLog.action == action)
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    
    total = query.count()
    logs = query.order_by(desc(AuditLog.timestamp)).offset(skip).limit(limit).all()
    
    return {
        "total": total,
        "logs": [
            {
                "id": log.id,
                "user_id": log.user_id,
                "username": db.query(User).filter(User.id == log.user_id).first().username if log.user_id else None,
                "action": log.action,
                "details": log.details,
                "ip_address": log.ip_address,
                "timestamp": log.timestamp.isoformat() if log.timestamp else None
            }
            for log in logs
        ]
    }

@app.get("/api/admin/dashboard-stats")
async def get_admin_stats(
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    """Get admin dashboard statistics"""
    total_docs = db.query(Document).count()
    pending_approvals = db.query(Document).filter(
        Document.status.in_([ApprovalStatus.PENDING_LEVEL1, ApprovalStatus.PENDING_LEVEL2, ApprovalStatus.PENDING_LEVEL3])
    ).count()
    approved_docs = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED).count()
    rejected_docs = db.query(Document).filter(Document.status == ApprovalStatus.REJECTED).count()
    duplicates = db.query(Document).filter(Document.is_duplicate == True).count()
    
    total_users = db.query(User).count()
    active_users = db.query(User).filter(User.is_active == True).count()
    
    # Recent activity
    recent_logs = db.query(AuditLog).order_by(desc(AuditLog.timestamp)).limit(10).all()
    
    # Monthly uploads
    six_months_ago = datetime.now(timezone.utc) - timedelta(days=180)
    monthly_uploads = db.query(
        func.strftime("%Y-%m", Document.upload_date).label("month"),
        func.count(Document.id).label("count")
    ).filter(Document.upload_date >= six_months_ago).group_by("month").order_by("month").all()
    
    return {
        "documents": {
            "total": total_docs,
            "pending_approvals": pending_approvals,
            "approved": approved_docs,
            "rejected": rejected_docs,
            "duplicates": duplicates
        },
        "users": {
            "total": total_users,
            "active": active_users
        },
        "recent_activity": [
            {
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                "action": log.action,
                "details": log.details
            }
            for log in recent_logs
        ],
        "monthly_uploads": [{"month": m[0], "count": m[1]} for m in monthly_uploads]
    }

# ==================== Health Check ====================
@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

# ==================== Static Files ====================
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
else:
    logger.warning("⚠️ Static directory not found. Create a 'static' folder with your HTML files.")

# ==================== Main Entry Point ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    logger.info("=" * 60)
    logger.info("📄 Document Management System Starting...")
    logger.info("=" * 60)
    logger.info(f"🌐 Server will run on: http://0.0.0.0:{port}")
    logger.info(f"📚 API Documentation: http://0.0.0.0:{port}/docs")
    logger.info(f"🗄️  Database: {Config.DATABASE_TYPE.upper()}")
    logger.info("=" * 60)
    
    uvicorn.run(
        "app:app", 
        host="0.0.0.0", 
        port=port, 
        reload=False
    )
