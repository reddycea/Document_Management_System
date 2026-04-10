-- Create database if it doesn't exist
CREATE DATABASE IF NOT EXISTS doc_management
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE doc_management;

-- ======================================================
-- Drop tables in reverse order of dependencies
-- ======================================================
DROP TABLE IF EXISTS audit_logs;
DROP TABLE IF EXISTS approvals;
DROP TABLE IF EXISTS documents;
DROP TABLE IF EXISTS users;

-- ======================================================
-- USERS TABLE
-- ======================================================
CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    role ENUM('ADMIN', 'APPROVER', 'MANAGER', 'VIEWER') NOT NULL,
    full_name VARCHAR(100),
    is_active BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_username (username),
    INDEX idx_email (email),
    INDEX idx_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ======================================================
-- DOCUMENTS TABLE
-- ======================================================
CREATE TABLE documents (
    id INT AUTO_INCREMENT PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    file_hash VARCHAR(64) UNIQUE,
    document_type VARCHAR(20) NOT NULL,
    vendor_name VARCHAR(200),
    invoice_number VARCHAR(100),
    invoice_date DATETIME,
    amount DECIMAL(15, 2),
    vat_amount DECIMAL(15, 2),
    tax_rate DECIMAL(5, 2),
    upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    uploaded_by INT NOT NULL,
    status ENUM('PENDING_LEVEL1', 'PENDING_LEVEL2', 'PENDING_LEVEL3', 'APPROVED', 'REJECTED')
        DEFAULT 'PENDING_LEVEL1',
    is_duplicate BOOLEAN DEFAULT FALSE,
    duplicate_reason TEXT,

    FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE RESTRICT,
    INDEX idx_invoice_number (invoice_number),
    INDEX idx_vendor_name (vendor_name),
    INDEX idx_status (status),
    INDEX idx_upload_date (upload_date),
    INDEX idx_amount (amount)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ======================================================
-- APPROVALS TABLE
-- ======================================================
CREATE TABLE approvals (
    id INT AUTO_INCREMENT PRIMARY KEY,
    document_id INT NOT NULL,
    approver_id INT NOT NULL,
    approval_level INT NOT NULL,
    decision VARCHAR(20) NOT NULL,
    comments TEXT,
    approved_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    FOREIGN KEY (approver_id) REFERENCES users(id) ON DELETE RESTRICT,
    INDEX idx_document_id (document_id),
    INDEX idx_approver_id (approver_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ======================================================
-- AUDIT LOGS TABLE
-- ======================================================
CREATE TABLE audit_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    action VARCHAR(100) NOT NULL,
    details TEXT,
    ip_address VARCHAR(45),
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    INDEX idx_action (action),
    INDEX idx_timestamp (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ======================================================
-- USEFUL VIEWS
-- ======================================================
CREATE OR REPLACE VIEW v_approved_documents AS
SELECT d.id, d.filename, d.vendor_name, d.invoice_number, d.invoice_date,
       d.amount, d.vat_amount, d.upload_date, u.username AS uploaded_by, d.status
FROM documents d
JOIN users u ON d.uploaded_by = u.id
WHERE d.status = 'APPROVED';

CREATE OR REPLACE VIEW v_pending_approvals AS
SELECT d.id, d.filename, d.vendor_name, d.amount, d.status, d.upload_date
FROM documents d
WHERE d.status IN ('PENDING_LEVEL1', 'PENDING_LEVEL2', 'PENDING_LEVEL3');

-- STORED PROCEDURE

DROP PROCEDURE IF EXISTS GetSpendingSummary;
CREATE PROCEDURE GetSpendingSummary(
    IN p_start_date DATE,
    IN p_end_date DATE,
    IN p_vendor_name VARCHAR(200)
)
BEGIN
    SELECT COALESCE(SUM(amount), 0) AS total_amount,
           COALESCE(SUM(vat_amount), 0) AS total_vat,
           COUNT(*) AS document_count,
           COUNT(DISTINCT vendor_name) AS unique_vendors
    FROM documents
    WHERE status = 'APPROVED'
      AND (p_start_date IS NULL OR invoice_date >= p_start_date)
      AND (p_end_date IS NULL OR invoice_date <= p_end_date)
      AND (p_vendor_name IS NULL OR vendor_name LIKE CONCAT('%', p_vendor_name, '%'));
END;

-- Insert default users (same as before)

INSERT IGNORE INTO users
(username, email, hashed_password, role, full_name, is_active, created_at)
VALUES
('admin', 'admin@system.com', '$2b$12$5QC3SbZYjlswRNRRx878q.rUSfJThqmwwtWtRIf9rJvGhN7DYVu.y', 'ADMIN', 'System Administrator', 1, NOW()),
('approver', 'approver@system.com', '$2b$12$CccxUPwXzHyZM8KPmSPm4OL/o6uZoqn48L62NTf/ulEIlj6OSe3k.', 'APPROVER', 'Level1 Approver', 1, NOW()),
('manager', 'manager@system.com', '$2b$12$cvuY1ah3NxHUle4jgBZlFO4r97Pdvm.KHRCP9qvpRjV5IJAWZiKL6', 'MANAGER', 'Level2 Manager', 1, NOW()),
('viewer', 'viewer@system.com', '$2b$12$OZlnEcUzhUNCwI2XIdIJGujrF.XtPoIw8BtPhNdxdzrX0PK27X756', 'VIEWER', 'Report Viewer', 1, NOW());

-- Insert documents with DYNAMIC dates (last 6 months)

-- Helper variables to make dates relative to now
SET @now = NOW();

-- 1. Pending Level 1 (2 documents, invoice dates within last 60 days)
INSERT INTO documents (filename, file_path, file_hash, document_type, vendor_name, invoice_number, invoice_date, amount, vat_amount, upload_date, uploaded_by, status, is_duplicate, duplicate_reason)
VALUES
('invoice_acme_12345.pdf', 'uploads/dummy1.pdf', 'hash1', 'invoice', 'ACME Corp', 'INV-2025-001', @now - INTERVAL 15 DAY, 1250.00, 250.00, @now - INTERVAL 14 DAY, 2, 'PENDING_LEVEL1', 0, NULL),
('invoice_globex_789.pdf', 'uploads/dummy2.pdf', 'hash2', 'invoice', 'Globex Industries', 'GX-9876', @now - INTERVAL 10 DAY, 3450.50, 690.10, @now - INTERVAL 9 DAY, 2, 'PENDING_LEVEL1', 0, NULL);

-- 2. Pending Level 2 (2 documents, invoice dates within last 90 days)
INSERT INTO documents (filename, file_path, file_hash, document_type, vendor_name, invoice_number, invoice_date, amount, vat_amount, upload_date, uploaded_by, status, is_duplicate, duplicate_reason)
VALUES
('invoice_wayne_ent.pdf', 'uploads/dummy3.pdf', 'hash3', 'invoice', 'Wayne Enterprises', 'WE-4455', @now - INTERVAL 45 DAY, 8900.00, 1780.00, @now - INTERVAL 44 DAY, 2, 'PENDING_LEVEL2', 0, NULL),
('credit_stark_note.pdf', 'uploads/dummy4.pdf', 'hash4', 'credit_note', 'Stark Industries', 'CN-2025-01', @now - INTERVAL 40 DAY, -500.00, -100.00, @now - INTERVAL 39 DAY, 3, 'PENDING_LEVEL2', 0, NULL);

-- 3. Pending Level 3 (2 documents, invoice dates within last 120 days)
INSERT INTO documents (filename, file_path, file_hash, document_type, vendor_name, invoice_number, invoice_date, amount, vat_amount, upload_date, uploaded_by, status, is_duplicate, duplicate_reason)
VALUES
('invoice_osci_corp.pdf', 'uploads/dummy5.pdf', 'hash5', 'invoice', 'Oscorp', 'OSC-999', @now - INTERVAL 80 DAY, 15200.00, 3040.00, @now - INTERVAL 79 DAY, 2, 'PENDING_LEVEL3', 0, NULL),
('credit_umbrella.pdf', 'uploads/dummy6.pdf', 'hash6', 'credit_note', 'Umbrella Corp', 'UBC-777', @now - INTERVAL 75 DAY, -320.00, -64.00, @now - INTERVAL 74 DAY, 3, 'PENDING_LEVEL3', 0, NULL);

-- 4. Approved documents (3 documents, spread over last 180 days)
INSERT INTO documents (filename, file_path, file_hash, document_type, vendor_name, invoice_number, invoice_date, amount, vat_amount, upload_date, uploaded_by, status, is_duplicate, duplicate_reason)
VALUES
('invoice_cyberdyne.pdf', 'uploads/dummy7.pdf', 'hash7', 'invoice', 'Cyberdyne Systems', 'CS-2024-999', @now - INTERVAL 150 DAY, 2300.00, 460.00, @now - INTERVAL 149 DAY, 2, 'APPROVED', 0, NULL),
('invoice_tyrell.pdf', 'uploads/dummy8.pdf', 'hash8', 'invoice', 'Tyrell Corp', 'TY-884', @now - INTERVAL 100 DAY, 6700.00, 1340.00, @now - INTERVAL 99 DAY, 3, 'APPROVED', 0, NULL),
('credit_wonka.pdf', 'uploads/dummy9.pdf', 'hash9', 'credit_note', 'Wonka Industries', 'CN-WON-12', @now - INTERVAL 60 DAY, -150.00, -30.00, @now - INTERVAL 59 DAY, 2, 'APPROVED', 0, NULL);

-- 5. Rejected documents (2 documents, within last 30 days)
INSERT INTO documents (filename, file_path, file_hash, document_type, vendor_name, invoice_number, invoice_date, amount, vat_amount, upload_date, uploaded_by, status, is_duplicate, duplicate_reason)
VALUES
('invoice_initech.pdf', 'uploads/dummy10.pdf', 'hash10', 'invoice', 'Initech', 'IN-123', @now - INTERVAL 25 DAY, 999.99, 200.00, @now - INTERVAL 24 DAY, 2, 'REJECTED', 0, NULL),
('credit_duplicate.pdf', 'uploads/dummy11.pdf', 'hash11', 'credit_note', 'ACME Corp', 'INV-2025-001', @now - INTERVAL 20 DAY, 1250.00, 250.00, @now - INTERVAL 19 DAY, 3, 'REJECTED', 1, 'Duplicate invoice number: INV-2025-001 (Document #1)');

-- 6. Duplicate detected (still in workflow, last 5 days)
INSERT INTO documents (filename, file_path, file_hash, document_type, vendor_name, invoice_number, invoice_date, amount, vat_amount, upload_date, uploaded_by, status, is_duplicate, duplicate_reason)
VALUES
('invoice_acme_duplicate2.pdf', 'uploads/dummy12.pdf', 'hash12', 'invoice', 'ACME Corp', 'INV-2025-099', @now - INTERVAL 5 DAY, 1250.00, 250.00, @now - INTERVAL 4 DAY, 2, 'PENDING_LEVEL1', 1, 'Possible duplicate: same vendor and amount found in document #1');


-- For document #7 (approved, ID 7) – level1, level2, level3 approvals
INSERT INTO approvals (document_id, approver_id, approval_level, decision, comments, approved_at)
VALUES
(7, 2, 1, 'approved', 'Looks good', @now - INTERVAL 148 DAY),
(7, 3, 2, 'approved', 'Within budget', @now - INTERVAL 147 DAY),
(7, 1, 3, 'approved', 'Final approval', @now - INTERVAL 146 DAY);

-- Document #8 (approved, ID 8)
INSERT INTO approvals (document_id, approver_id, approval_level, decision, comments, approved_at)
VALUES
(8, 2, 1, 'approved', 'OK', @now - INTERVAL 98 DAY),
(8, 3, 2, 'approved', 'Proceed', @now - INTERVAL 97 DAY),
(8, 1, 3, 'approved', 'Approved', @now - INTERVAL 96 DAY);

-- Document #9 (credit note approved, ID 9)
INSERT INTO approvals (document_id, approver_id, approval_level, decision, comments, approved_at)
VALUES
(9, 2, 1, 'approved', 'Valid credit', @now - INTERVAL 58 DAY),
(9, 3, 2, 'approved', 'OK', @now - INTERVAL 57 DAY),
(9, 1, 3, 'approved', 'Approved', @now - INTERVAL 56 DAY);

-- Document #10 (rejected, ID 10) – rejected at level 2
INSERT INTO approvals (document_id, approver_id, approval_level, decision, comments, approved_at)
VALUES
(10, 2, 1, 'approved', 'Pass to manager', @now - INTERVAL 23 DAY),
(10, 3, 2, 'rejected', 'Vendor not authorized', @now - INTERVAL 22 DAY);

-- Document #11 (rejected due to duplicate, ID 11) – rejected at level 1
INSERT INTO approvals (document_id, approver_id, approval_level, decision, comments, approved_at)
VALUES
(11, 2, 1, 'rejected', 'Duplicate invoice', @now - INTERVAL 18 DAY);

-- ======================================================
-- Audit logs (optional, with dynamic timestamps)
-- ======================================================
INSERT INTO audit_logs (user_id, action, details, ip_address, timestamp)
VALUES
(1, 'LOGIN', 'Admin logged in', '127.0.0.1', @now - INTERVAL 30 DAY),
(2, 'UPLOAD', 'Uploaded invoice_acme_12345.pdf', '127.0.0.1', @now - INTERVAL 14 DAY),
(2, 'APPROVAL', 'Approved document #7 level1', '127.0.0.1', @now - INTERVAL 148 DAY),
(3, 'APPROVAL', 'Approved document #7 level2', '127.0.0.1', @now - INTERVAL 147 DAY),
(1, 'APPROVAL', 'Approved document #7 level3', '127.0.0.1', @now - INTERVAL 146 DAY),
(3, 'UPLOAD', 'Uploaded credit_stark_note.pdf', '127.0.0.1', @now - INTERVAL 39 DAY),
(1, 'REPORT_EXPORT', 'Exported spend report as Excel', '127.0.0.1', @now - INTERVAL 5 DAY);