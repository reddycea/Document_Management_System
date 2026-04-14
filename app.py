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

# FastAPI and dependencies
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, status, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Enum as SQLEnum, ForeignKey, Boolean, Text, and_, func, or_, Index
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from jose import JWTError, jwt
from passlib.context import CryptContext

# Document processing
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== Configuration ====================
class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this-in-production")
    if not SECRET_KEY or SECRET_KEY == "your-secret-key-change-this-in-production":
        logger.warning("⚠️ WARNING: Using default SECRET_KEY. Set a secure key in .env file!")

    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 30))

    # PostgreSQL Configuration
    PG_HOST = os.getenv("PG_HOST", "localhost")
    PG_PORT = os.getenv("PG_PORT", "5432")
    PG_USER = os.getenv("PG_USER", "postgres")
    PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres")
    PG_DB = os.getenv("PG_DB", "doc_management")

    # MySQL Configuration
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB = os.getenv("MYSQL_DB", "doc_management")

    # Database selection
    DATABASE_TYPE = os.getenv("DATABASE_TYPE", "sqlite").lower()  # Changed to sqlite as default for easier testing
    
    # Render.com PostgreSQL URL
    RENDER_DATABASE_URL = os.getenv("DATABASE_URL")
    
    SQLITE_URL = "sqlite:///./doc_management.db"

    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 10 * 1024 * 1024))
    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

    @classmethod
    def get_database_url(cls):
        if cls.RENDER_DATABASE_URL:
            logger.info(f"✅ Using Render.com PostgreSQL database")
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
            logger.info("⚠️ Using SQLite database (development only)")
            return cls.SQLITE_URL

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
    username = Column(String(50), unique=True, nullable=False, index=True)
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
    extraction_confidence = Column(Float, default=0.0)
    upload_date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    status = Column(SQLEnum(ApprovalStatus), default=ApprovalStatus.PENDING_LEVEL1)
    is_duplicate = Column(Boolean, default=False)
    duplicate_reason = Column(Text)
    validation_errors = Column(Text)
    uploader = relationship("User")
    
    # Add indexes for better performance
    __table_args__ = (
        Index('idx_vendor_date', 'vendor_name', 'invoice_date'),
        Index('idx_status_upload', 'status', 'upload_date'),
    )

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
            pool_recycle=3600,
            echo=False
        )
    else:
        # SQLite
        engine = create_engine(
            database_url, 
            connect_args={"check_same_thread": False} if "sqlite" in database_url else {}
        )
    
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    logger.info("✅ Database engine created successfully")
    
except Exception as e:
    logger.error(f"❌ Database connection error: {e}")
    logger.info("⚠️ Falling back to SQLite...")
    Config.DATABASE_TYPE = "sqlite"
    engine = create_engine(Config.SQLITE_URL, connect_args={"check_same_thread": False})
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
# Use bcrypt explicitly with proper configuration
pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=12, deprecated="auto")
security = HTTPBearer(auto_error=False)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password with better error handling"""
    try:
        if not hashed_password:
            logger.error("No hashed password provided")
            return False
        
        # Check if it's a valid bcrypt hash format
        if not hashed_password.startswith('$2b$'):
            logger.warning(f"Invalid hash format (expected bcrypt): {hashed_password[:10]}...")
            # Rehash the password if it's in wrong format (for backward compatibility)
            return False
        
        result = pwd_context.verify(plain_password, hashed_password)
        if not result:
            logger.warning(f"Password verification failed")
        return result
    except Exception as e:
        logger.error(f"Password verification error: {e}")
        return False

def get_password_hash(password: str) -> str:
    """Hash password with bcrypt"""
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
    except JWTError as e:
        logger.error(f"JWT Error: {e}")
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

# ==================== OCR Pipeline: Clean & Structure ====================
class ImagePreprocessor:
    """Clean and preprocess images for better OCR accuracy"""
    
    @staticmethod
    def clean_image(image: Image.Image) -> Image.Image:
        """Apply image cleaning techniques using PIL"""
        # Convert to grayscale if needed
        if image.mode != 'L':
            image = image.convert('L')
        
        # Enhance contrast
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0)
        
        # Enhance sharpness
        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(2.0)
        
        # Apply slight blur to reduce noise
        image = image.filter(ImageFilter.MedianFilter(size=3))
        
        # Apply thresholding (binarization)
        threshold = 128
        image = image.point(lambda p: 255 if p > threshold else 0)
        
        return image
    
    @staticmethod
    def enhance_resolution(image: Image.Image, scale: int = 2) -> Image.Image:
        """Enhance image resolution using interpolation"""
        width, height = image.size
        new_size = (width * scale, height * scale)
        return image.resize(new_size, Image.Resampling.LANCZOS)

class OCRProcessor:
    """Handle OCR text extraction with preprocessing pipeline"""
    
    @staticmethod
    def extract_text(image_path: str, preprocess: bool = True) -> str:
        """Extract text from image with preprocessing"""
        try:
            image = Image.open(image_path)
            
            if preprocess:
                image = ImagePreprocessor.clean_image(image)
                image = ImagePreprocessor.enhance_resolution(image, scale=2)
            
            # Configure Tesseract for better accuracy
            custom_config = r'--oem 3 --psm 6'
            text = pytesseract.image_to_string(image, config=custom_config)
            text = OCRProcessor._clean_text(text)
            
            logger.info(f"OCR extracted {len(text)} characters from {image_path}")
            return text
            
        except Exception as e:
            logger.error(f"OCR failed for {image_path}: {e}")
            return ""
    
    @staticmethod
    def extract_text_from_pdf(pdf_path: str, preprocess: bool = True) -> str:
        """Extract text from PDF using pdf2image and OCR at 300 DPI"""
        try:
            # Convert PDF to images at 300 DPI for better quality
            images = pdf2image.convert_from_path(pdf_path, dpi=300)
            all_text = []
            
            for i, image in enumerate(images):
                logger.info(f"Processing PDF page {i+1}/{len(images)}")
                
                if preprocess:
                    image = ImagePreprocessor.clean_image(image)
                    image = ImagePreprocessor.enhance_resolution(image, scale=2)
                
                custom_config = r'--oem 3 --psm 6'
                text = pytesseract.image_to_string(image, config=custom_config)
                all_text.append(OCRProcessor._clean_text(text))
            
            return "\n".join(all_text)
            
        except Exception as e:
            logger.error(f"PDF OCR failed for {pdf_path}: {e}")
            return ""
    
    @staticmethod
    def _clean_text(text: str) -> str:
        """Clean extracted text"""
        # Remove extra whitespace
        text = re.sub(r'\s+', ' ', text)
        # Remove non-printable characters
        text = re.sub(r'[^\x20-\x7E\n]', '', text)
        # Fix common OCR errors
        replacements = {'0': 'O', '1': 'I', '|': 'I', '!': 'I'}
        for wrong, correct in replacements.items():
            text = text.replace(wrong, correct)
        return text.strip()

# ==================== AI Extraction ====================
def parse_date(date_str: str) -> Optional[datetime]:
    """Parse date from various formats"""
    date_formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
        "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y",
        "%b %d, %Y", "%d %b %Y", "%B %d, %Y"
    ]
    for fmt in date_formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None

class AIExtractor:
    """AI-powered document field extraction with confidence scoring"""
    
    EXTRACTION_PATTERNS = {
        "vendor_name": {
            "patterns": [
                r"(?:Vendor|Supplier|Company|Bill From|Seller|Issuer)[\s:]+([A-Za-z0-9\s&.,]+)(?:\n|$)",
                r"(?:From|Sold by)[\s:]+([A-Za-z0-9\s&.,]+)(?:\n|$)",
                r"^([A-Za-z\s&.,]+)(?:\n|$)",
            ],
            "weight": 0.9,
            "validation": lambda x: len(x) > 2 and len(x) < 100
        },
        "invoice_number": {
            "patterns": [
                r"(?:Invoice|Document|Bill)[\s#:]+([A-Z0-9\-]+)",
                r"(?:INV|INVOICE)[\s\-]*([A-Z0-9\-]+)",
                r"Number[\s:]+([A-Z0-9\-]+)",
            ],
            "weight": 1.0,
            "validation": lambda x: len(x) > 3 and len(x) < 50
        },
        "invoice_date": {
            "patterns": [
                r"(?:Date|Invoice Date|Issue Date)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
                r"(?:Date|Invoice Date|Issue Date)[\s:]+(\d{4}-\d{2}-\d{2})",
                r"(\d{1,2}/\d{1,2}/\d{4})",
            ],
            "weight": 0.95,
            "validation": lambda x: True,
            "parser": parse_date
        },
        "amount": {
            "patterns": [
                r"(?:Total|Amount Due|Grand Total|Balance Due)[\s:]*[$]?([\d,]+\.?\d*)",
                r"TOTAL\s+[$]?([\d,]+\.?\d*)",
                r"(?:Sum|Net Amount)[\s:]*[$]?([\d,]+\.?\d*)",
            ],
            "weight": 0.95,
            "validation": lambda x: x > 0 and x < 10000000,
            "parser": lambda x: float(x.replace(",", ""))
        },
        "vat_amount": {
            "patterns": [
                r"(?:VAT|Tax|GST|HST)[\s:]*[$]?([\d,]+\.?\d*)",
                r"Tax Amount\s+[$]?([\d,]+\.?\d*)",
                r"(?:VAT|Tax)[\s:]+(\d+\.?\d*)%",
            ],
            "weight": 0.85,
            "validation": lambda x: x >= 0 and x < 1000000,
            "parser": lambda x: float(x.replace(",", "")) if not x.endswith('%') else None
        },
        "tax_rate": {
            "patterns": [
                r"(?:VAT|Tax|GST) Rate[\s:]+(\d+\.?\d*)%",
                r"Tax\s+(\d+\.?\d*)%",
            ],
            "weight": 0.7,
            "validation": lambda x: 0 <= x <= 100,
            "parser": lambda x: float(x.replace("%", ""))
        }
    }
    
    @classmethod
    async def extract_from_image(cls, image_path: str) -> Dict[str, Any]:
        """Extract fields from image with confidence scoring"""
        text = OCRProcessor.extract_text(image_path, preprocess=True)
        return await cls._extract_fields(text)
    
    @classmethod
    async def extract_from_pdf(cls, pdf_path: str) -> Dict[str, Any]:
        """Extract fields from PDF with confidence scoring"""
        text = OCRProcessor.extract_text_from_pdf(pdf_path, preprocess=True)
        return await cls._extract_fields(text)
    
    @classmethod
    async def _extract_fields(cls, text: str) -> Dict[str, Any]:
        """Core extraction logic with multi-pattern matching and confidence"""
        extracted = {}
        field_confidences = {}
        
        for field, config in cls.EXTRACTION_PATTERNS.items():
            best_match = None
            best_confidence = 0
            
            for pattern in config["patterns"]:
                matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
                for match in matches:
                    value = match if isinstance(match, str) else match[0]
                    value = value.strip()
                    
                    if "parser" in config:
                        try:
                            value = config["parser"](value)
                        except:
                            continue
                    
                    if config["validation"] and not config["validation"](value):
                        continue
                    
                    pattern_confidence = cls._calculate_pattern_confidence(pattern, str(match), text)
                    confidence = config["weight"] * pattern_confidence
                    
                    if confidence > best_confidence:
                        best_match = value
                        best_confidence = confidence
            
            if best_match is not None:
                extracted[field] = best_match
                field_confidences[field] = round(best_confidence, 2)
        
        # Post-processing and cross-field validation
        if extracted.get("amount") and extracted.get("tax_rate") and not extracted.get("vat_amount"):
            extracted["vat_amount"] = round(extracted["amount"] * extracted["tax_rate"] / 100, 2)
        
        if extracted.get("amount") and extracted.get("vat_amount"):
            if extracted["vat_amount"] > extracted["amount"]:
                extracted["vat_amount"] = None
        
        overall_confidence = cls._calculate_overall_confidence(extracted, field_confidences)
        
        return {
            **extracted,
            "confidence": overall_confidence,
            "field_confidences": field_confidences
        }
    
    @staticmethod
    def _calculate_pattern_confidence(pattern: str, match: str, full_text: str) -> float:
        """Calculate confidence based on pattern match quality"""
        confidence = 0.8
        # Boost if match appears near start of text
        match_pos = full_text.find(match)
        if match_pos >= 0 and match_pos < 200:
            confidence += 0.1
        if len(match) < 3:
            confidence -= 0.2
        return min(confidence, 1.0)
    
    @staticmethod
    def _calculate_overall_confidence(extracted: Dict, field_confidences: Dict) -> float:
        """Calculate overall confidence score"""
        field_weights = {
            "vendor_name": 0.15,
            "invoice_number": 0.20,
            "invoice_date": 0.20,
            "amount": 0.30,
            "vat_amount": 0.15
        }
        
        total_weight = 0
        weighted_score = 0
        
        for field, weight in field_weights.items():
            if extracted.get(field) is not None:
                if field in field_confidences:
                    weighted_score += weight * field_confidences[field]
                else:
                    weighted_score += weight
            total_weight += weight
        
        return round(weighted_score / total_weight, 2) if total_weight > 0 else 0.0

# ==================== Validation ====================
class DataValidator:
    """Enhanced data validation with business rules"""
    
    @staticmethod
    def validate_extraction(data: Dict[str, Any]) -> Tuple[bool, List[str], Dict[str, Any]]:
        """Validate extracted data and return validation results"""
        errors = []
        corrected = data.copy()
        
        if data.get("amount"):
            if data["amount"] < 0:
                errors.append("Amount cannot be negative")
                corrected["amount"] = None
            elif data["amount"] > 10000000:
                errors.append(f"Amount ${data['amount']:,.2f} exceeds typical maximum")
        
        if data.get("vat_amount"):
            if data["vat_amount"] < 0:
                errors.append("VAT amount cannot be negative")
                corrected["vat_amount"] = None
        
        if data.get("amount") and data.get("vat_amount"):
            vat_ratio = data["vat_amount"] / data["amount"]
            if vat_ratio > 0.3:
                errors.append(f"VAT amount (${data['vat_amount']:,.2f}) seems high for amount (${data['amount']:,.2f})")
        
        if data.get("invoice_date"):
            if data["invoice_date"] > datetime.now(timezone.utc):
                errors.append("Invoice date cannot be in the future")
                corrected["invoice_date"] = None
        
        if data.get("vendor_name"):
            cleaned = re.sub(r'[^a-zA-Z\s\.&]', '', data["vendor_name"])
            if len(cleaned) < 2:
                errors.append("Vendor name appears invalid")
                corrected["vendor_name"] = None
            else:
                corrected["vendor_name"] = cleaned
        
        return len(errors) == 0, errors, corrected

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
    logger.info("🚀 Starting Document Management System...")
    logger.info(f"📊 Database Type: {Config.DATABASE_TYPE}")
    
    if engine:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Database tables created/verified")
    
    os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
    logger.info(f"✅ Upload directory: {Config.UPLOAD_DIR}")
    
    db = SessionLocal()
    try:
        # Check if users exist
        user_count = db.query(User).count()
        
        if user_count == 0:
            logger.info("No users found. Creating default users...")
            default_users = [
                ("admin", "admin@system.com", "Admin@123", UserRole.ADMIN, "System Administrator"),
                ("approver", "approver@system.com", "Approver@123", UserRole.APPROVER, "Level1 Approver"),
                ("manager", "manager@system.com", "Manager@123", UserRole.MANAGER, "Level2 Manager"),
                ("viewer", "viewer@system.com", "Viewer@123", UserRole.VIEWER, "Report Viewer")
            ]
            for username, email, pwd, role, fullname in default_users:
                user = db.query(User).filter(User.username == username).first()
                if not user:
                    hashed_pw = get_password_hash(pwd)
                    logger.info(f"Creating user {username} with hash: {hashed_pw[:20]}...")
                    user = User(
                        username=username,
                        email=email,
                        hashed_password=hashed_pw,
                        role=role,
                        full_name=fullname,
                        is_active=True
                    )
                    db.add(user)
            db.commit()
            logger.info("✅ Default users created successfully")
            
            # Verify users were created
            new_count = db.query(User).count()
            logger.info(f"Total users after creation: {new_count}")
        else:
            logger.info(f"✅ Users already exist ({user_count} users found)")
            
            # Verify admin user specifically
            admin = db.query(User).filter(User.username == "admin").first()
            if admin:
                logger.info(f"Admin user found: {admin.username}, Role: {admin.role}")
                # Test password verification
                test_result = verify_password("Admin@123", admin.hashed_password)
                logger.info(f"Admin password verification test: {test_result}")
            else:
                logger.warning("Admin user not found!")
                
    except Exception as e:
        logger.error(f"⚠️ Startup error: {e}")
        db.rollback()
    finally:
        db.close()
    
    print("\n" + "=" * 60)
    print("📄 Document Management System Ready!")
    print("=" * 60)
    print("\n🔐 Default Login Credentials:")
    print("  👑 Admin:    admin / Admin@123")
    print("  ✅ Approver: approver / Approver@123")
    print("  📊 Manager:  manager / Manager@123")
    print("  👁️ Viewer:   viewer / Viewer@123")
    print("\n📚 API Documentation: http://localhost:8000/docs")
    print("=" * 60 + "\n")
    
    yield
    logger.info("🛑 Shutting down...")

# ==================== FastAPI App ====================
app = FastAPI(
    title="DocManager", 
    version="3.0", 
    description="Document Management System with OCR, Approval Workflow, and Analytics",
    lifespan=lifespan
)

# Configure CORS - restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],  # Restrict for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return RedirectResponse(url="/docs")  # Redirect to API docs instead

# ==================== Authentication Routes ====================
@app.post("/api/auth/login", response_model=Token)
async def login(
    request: Request,
    response: Response, 
    user_login: UserLogin, 
    db: Session = Depends(get_db)
):
    logger.info(f"Login attempt for username: {user_login.username}")
    
    # Debug: Check if user exists
    user = db.query(User).filter(User.username == user_login.username).first()
    
    if not user:
        logger.warning(f"User not found: {user_login.username}")
        # Check if any users exist for debugging
        user_count = db.query(User).count()
        logger.info(f"Total users in database: {user_count}")
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    logger.info(f"User found: {user.username}, Role: {user.role.value if user.role else 'None'}, Active: {user.is_active}")
    logger.info(f"Stored hash prefix: {user.hashed_password[:30] if user.hashed_password else 'None'}...")
    
    # Test verification
    is_valid = verify_password(user_login.password, user.hashed_password)
    logger.info(f"Password verification result: {is_valid}")
    
    if not is_valid or not user.is_active:
        logger.warning(f"Login failed for {user.username}: valid={is_valid}, active={user.is_active}")
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
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
    
    audit = AuditLog(
        user_id=user.id, 
        action="LOGIN", 
        details="User logged in", 
        ip_address=request.client.host if hasattr(request, 'client') else "unknown"
    )
    db.add(audit)
    db.commit()
    
    logger.info(f"✅ User {user.username} logged in successfully")
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

# ==================== Debug Endpoints (Remove in production) ====================
@app.get("/debug/check-users")
async def check_users(db: Session = Depends(get_db)):
    """Debug endpoint to check users (remove in production)"""
    users = db.query(User).all()
    return {
        "user_count": len(users),
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "role": u.role.value if u.role else None,
                "is_active": u.is_active,
                "hashed_password_prefix": u.hashed_password[:30] if u.hashed_password else None,
                "created_at": u.created_at
            }
            for u in users
        ]
    }

@app.get("/debug/db-status")
async def db_status(db: Session = Depends(get_db)):
    """Debug endpoint to check database status"""
    try:
        # Test connection
        if Config.DATABASE_TYPE == "sqlite":
            db.execute("SELECT 1")
        else:
            db.execute("SELECT 1")
        
        # Check tables
        from sqlalchemy import inspect
        inspector = inspect(db.get_bind())
        tables = inspector.get_table_names()
        
        # Count users
        user_count = db.query(User).count()
        
        return {
            "connected": True,
            "tables": tables,
            "user_count": user_count,
            "database_type": Config.DATABASE_TYPE
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
            "database_type": Config.DATABASE_TYPE
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
    
    # OCR Pipeline: Extract -> Validate -> Score Confidence
    try:
        if ext == ".pdf":
            extracted = await AIExtractor.extract_from_pdf(file_path)
        else:
            extracted = await AIExtractor.extract_from_image(file_path)
        
        is_valid, validation_errors, corrected_data = DataValidator.validate_extraction(extracted)
        
        if not is_valid:
            logger.warning(f"Validation warnings for {file.filename}: {validation_errors}")
        
    except Exception as e:
        logger.error(f"Extraction failed for {file.filename}: {e}")
        extracted = {
            "vendor_name": None,
            "invoice_number": None,
            "invoice_date": None,
            "amount": None,
            "vat_amount": None,
            "tax_rate": None,
            "confidence": 0.0,
            "field_confidences": {}
        }
        corrected_data = extracted
        validation_errors = [f"Extraction failed: {str(e)}"]
    
    is_dup, dup_reason = DuplicateDetector.check_duplicate(
        db, corrected_data.get("invoice_number"), corrected_data.get("vendor_name"),
        corrected_data.get("amount"), content, document_type
    )
    
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
        extraction_confidence=extracted.get("confidence", 0.0),
        uploaded_by=current_user.id,
        status=ApprovalStatus.PENDING_LEVEL1,
        is_duplicate=is_dup,
        duplicate_reason=dup_reason,
        validation_errors="; ".join(validation_errors) if validation_errors else None
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    
    audit = AuditLog(
        user_id=current_user.id, 
        action="UPLOAD", 
        details=f"Uploaded {file.filename} (ID: {doc.id}) - Confidence: {extracted.get('confidence', 0)}", 
        ip_address=request.client.host if hasattr(request, 'client') else "unknown"
    )
    db.add(audit)
    db.commit()
    
    return {
        "message": "Document uploaded",
        "document_id": doc.id,
        "extracted_data": {
            **corrected_data,
            "confidence": extracted.get("confidence", 0),
            "field_confidences": extracted.get("field_confidences", {})
        },
        "validation_warnings": validation_errors if validation_errors else None,
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
            "is_duplicate": d.is_duplicate,
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
            "tax_rate": doc.tax_rate,
            "extraction_confidence": doc.extraction_confidence,
            "status": doc.status.value,
            "upload_date": doc.upload_date,
            "is_duplicate": doc.is_duplicate,
            "duplicate_reason": doc.duplicate_reason,
            "validation_errors": doc.validation_errors
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

# ==================== Approval Routes ====================
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
            "upload_date": d.upload_date,
            "extraction_confidence": d.extraction_confidence
        }
        for d in docs
    ]

# ==================== Report Routes (Abbreviated for brevity) ====================
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
            "unique_vendors": len(vendor_breakdown),
            "average_confidence": sum(d.extraction_confidence or 0 for d in docs) / len(docs) if docs else 0
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
                "confidence": d.extraction_confidence
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
            "Extraction Confidence": d.extraction_confidence or 0,
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
                ["Average Amount", df["Amount"].mean()],
                ["Average Confidence", df["Extraction Confidence"].mean()]
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
    avg_confidence = sum(d.extraction_confidence or 0 for d in docs) / len(docs) if docs else 0
    elements.append(Paragraph(f"<b>Total Amount:</b> ${total_amount:,.2f}", styles['Normal']))
    elements.append(Paragraph(f"<b>Number of Documents:</b> {len(docs)}", styles['Normal']))
    elements.append(Paragraph(f"<b>Average Extraction Confidence:</b> {avg_confidence:.1%}", styles['Normal']))
    elements.append(Paragraph("<br/>", styles['Normal']))
    
    table_data = [["ID", "Vendor", "Invoice #", "Date", "Amount", "VAT", "Confidence"]]
    for d in docs:
        table_data.append([
            str(d.id),
            (d.vendor_name or "")[:30],
            (d.invoice_number or "")[:20],
            d.invoice_date.strftime("%Y-%m-%d") if d.invoice_date else "",
            f"${d.amount:,.2f}" if d.amount else "$0.00",
            f"${d.vat_amount:,.2f}" if d.vat_amount else "$0.00",
            f"{d.extraction_confidence:.1%}" if d.extraction_confidence else "N/A"
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
                    "confidence": d.extraction_confidence,
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
    
    low_conf_docs = [d for d in docs if d.extraction_confidence and d.extraction_confidence < 0.6]
    if low_conf_docs:
        insights.append(f"⚠️ {len(low_conf_docs)} documents have low extraction confidence (<60%) - review recommended")
    
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
            "total_vat": total_vat,
            "average_confidence": np.mean([d.extraction_confidence or 0 for d in docs])
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

@app.get("/api/admin/stats")
async def get_admin_stats(
    current_user: User = Depends(role_required([UserRole.ADMIN])),
    db: Session = Depends(get_db)
):
    total_docs = db.query(Document).count()
    approved_docs = db.query(Document).filter(Document.status == ApprovalStatus.APPROVED).count()
    pending_docs = db.query(Document).filter(Document.status.in_([
        ApprovalStatus.PENDING_LEVEL1, ApprovalStatus.PENDING_LEVEL2, ApprovalStatus.PENDING_LEVEL3
    ])).count()
    rejected_docs = db.query(Document).filter(Document.status == ApprovalStatus.REJECTED).count()
    
    avg_confidence = db.query(func.avg(Document.extraction_confidence)).scalar() or 0
    low_confidence_docs = db.query(Document).filter(Document.extraction_confidence < 0.6).count()
    
    return {
        "documents": {
            "total": total_docs,
            "approved": approved_docs,
            "pending": pending_docs,
            "rejected": rejected_docs
        },
        "extraction_quality": {
            "average_confidence": round(avg_confidence, 2),
            "low_confidence_count": low_confidence_docs,
            "low_confidence_percentage": round(low_confidence_docs / total_docs * 100, 2) if total_docs > 0 else 0
        }
    }

# ==================== Document Preview Endpoint ====================

@app.get("/api/documents/{document_id}/preview")
async def preview_document(
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Preview the actual document file"""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    
    if current_user.role == UserRole.VIEWER and doc.status != ApprovalStatus.APPROVED:
        raise HTTPException(403, "Access denied - document not approved")
    
    if not os.path.exists(doc.file_path):
        raise HTTPException(404, "File not found on server")
    
    # Determine media type
    ext = os.path.splitext(doc.filename)[1].lower()
    media_type = {
        '.pdf': 'application/pdf',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png'
    }.get(ext, 'application/octet-stream')
    
    def iterfile():
        with open(doc.file_path, mode="rb") as file_like:
            yield from file_like
    
    return StreamingResponse(
        iterfile(),
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename={doc.filename}"}
    )

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc)}

# ==================== Main ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("\n" + "=" * 60)
    print("📄 Document Management System Starting...")
    print("=" * 60)
    print(f"🌐 Server will run on: http://0.0.0.0:{port}")
    print(f"📚 API Documentation: http://0.0.0.0:{port}/docs")
    print(f"🗄️  Database: {Config.DATABASE_TYPE.upper()}")
    print("=" * 60)
    print("📋 OCR Pipeline: OCR → Clean → Structure → AI Extraction → Validation → Confidence")
    print("=" * 60 + "\n")
    
    uvicorn.run(
        "app:app", 
        host="0.0.0.0", 
        port=port, 
        reload=False
    )
