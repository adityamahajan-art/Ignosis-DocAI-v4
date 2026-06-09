import os
import time
import io
import json
import zipfile
import requests
import pytesseract
import pdfplumber
import re
import logging
import sys
from pdf2image import convert_from_bytes
from flask import Flask, request, jsonify, render_template
from pypdf import PdfReader, PdfWriter
from dotenv import load_dotenv  # 🔥 IMPORTED DOTENV

# 🔥 LOAD ENVIRONMENT VARIABLES FROM .env FILE
load_dotenv()

# Configure logging to write to both stdout and app.log
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    handlers=[
        logging.FileHandler("app.log", mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("docai")

# Import our dedicated engines
from financial_processor import FinancialStatementEngine
from local_extractor import extract_locally, STATUTORY_DOC_TYPES
from rule_engine import RuleEngine

app = Flask(__name__, template_folder='templates')

# 🔥 SECURE KEY LOADING: Pulls strictly from .env and throws an error if missing
SARVAM_API_KEY = os.environ.get('SARVAM_API_KEY')
if not SARVAM_API_KEY:
    raise ValueError("CRITICAL ERROR: SARVAM_API_KEY is missing! Please ensure you have created a .env file with your key.")

SARVAM_CHAT_ENDPOINT = 'https://api.sarvam.ai/v1/chat/completions'
SARVAM_DOC_ENDPOINT = 'https://api.sarvam.ai/doc-digitization/job/v1'

# Initialize the Rule Engine
rule_engine = RuleEngine("UnsecuredLogin_KSF_Rulebook.xlsx")

# =====================================================================
# DYNAMIC SCHEMA DICTIONARY (For Non-Statutory Documents)
# =====================================================================
DOCUMENT_SCHEMAS = {
    "Utility Bill / Invoice": '"consumer_number", "billing_name", "billing_address", "bill_date", "due_date", "bill_amount", "service_type"',
    "Property Tax Receipt": '"property_address", "owner_name", "tax_amount", "receipt_number", "payment_date", "financial_year"',
    "Rent Agreement": '"landlord_name", "tenant_name", "property_address", "monthly_rent", "lease_start_date", "lease_end_date"',
    "Bank Statement": '"account_name", "account_number", "bank_name", "branch_name", "ifsc_code", "statement_period", "account_address"',
    "Loan Agreement": '"account_holder_name", "bank_name", "account_number", "ifsc_code", "loan_amount", "agreement_date"',
    "Partnership Deed": '"partnership_name", "partner_names", "principal_place_of_business", "date_of_execution", "profit_sharing_ratio"',
    "Shop & Establishment Certificate": '"establishment_name", "employer_name", "registration_number", "category_of_establishment", "validity_date", "establishment_address"',
    "Tax Audit Report": '"entity_name", "assessment_year", "auditor_name", "ca_membership_number"',
    "Salary Slip": '"employee_name", "employer_name", "salary_month_year", "gross_salary", "net_salary", "provident_fund_deduction"',
    "Appointment Letter": '"employee_name", "employer_name", "designation", "date_of_joining", "ctc_or_salary"',
    "Financial Statement": '"entity_name", "financial_year", "total_assets", "total_equity_and_liabilities", "revenue_from_operations", "profit_after_tax", "net_cash_flow"',
    "MOA / AOA": '"company_name", "cin", "state_of_registration", "authorized_share_capital", "registered_office_address"',
    "PAN Card": '"pan_number", "full_name", "father_name", "date_of_birth", "taxpayer_status"',
    "General Document": '"entity_name", "document_date", "primary_amount", "key_reference_number"'
}

ITERATIVE_DOCS = ["Rent Agreement", "Loan Agreement", "Partnership Deed", "MOA / AOA", "Tax Audit Report"]

# 🔥 GLOBAL DATE FORMATTER: Forces DD/MM/YYYY with Slashes
def _format_date_with_slashes(date_str):
    if not date_str: return None
    s = str(date_str).strip()
    
    if re.match(r'^\d{2}/\d{2}/\d{4}$', s): 
        return s
        
    if re.match(r'^\d{2}-\d{2}-\d{4}$', s):
        return s.replace('-', '/')
        
    if re.match(r'^(19|20)\d{2}[/\-\.]((19|20)\d{2}|\d{2})$', s): 
        return s

    digits = re.sub(r'\D', '', s)
    
    if len(digits) == 8:
        if digits.startswith(('19', '20')) and int(digits[4:6]) <= 12 and int(digits[6:8]) <= 31:
            return f"{digits[6:8]}/{digits[4:6]}/{digits[0:4]}"
        return f"{digits[0:2]}/{digits[2:4]}/{digits[4:8]}"
        
    if len(digits) == 6:
        return f"{digits[0:2]}/{digits[2:4]}/20{digits[4:6]}"

    m1 = re.search(r'\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})\b', s)
    if m1:
        dd, mm, yy = m1.groups()
        dd = dd.zfill(2); mm = mm.zfill(2)
        if len(yy) == 2: yy = "20" + yy if int(yy) < 50 else "19" + yy
        return f"{dd}/{mm}/{yy}"

    return s

def generate_system_prompt(doc_type):
    target_fields = DOCUMENT_SCHEMAS.get(doc_type, DOCUMENT_SCHEMAS["General Document"])
    
    coercion_rules = ""
    if doc_type == "Property Tax Receipt":
        coercion_rules = """\n### COERCION RULES FOR PROPERTY TAX RECEIPT:
- "receipt_number" MUST extract the Receipt No, Transaction ID, Acknowledgement No, or Challan No.
- "tax_amount" MUST extract ONLY the final numerical amount paid/payable (e.g., "117.0", "3544"). STRIP OUT all alphabetic text like "E. and O.E.", "Rs.", or "Rupees".
- "financial_year" MUST extract the specific billing period or year (e.g., "2023-2024", "2021-22", "2024-2025").
- "owner_name" MUST extract the human or entity name next to Owner, Assessee Name, or "Received From". Do not include the label itself.
"""
    elif doc_type == "Shop & Establishment Certificate":
        coercion_rules = """\n### COERCION RULES FOR SHOP & ESTABLISHMENT CERTIFICATE:
- IMPORTANT: These certificates are often heavily localized in regional languages (e.g., Marathi, Kannada, Hindi). Translate and map contextually.
- "establishment_name" MUST map to "Name of the Establishment", "आस्थापनेचे नाव", "ಸಂಸ್ಥೆಯ ಹೆಸರು", or similar headings.
- "employer_name" MUST map to "Name of the Employer", "मालकाचे नाव", "ಮಾಲೀಕರ ಹೆಸರು", or Proprietor/Owner.
- "registration_number" MUST map to Registration No, "पावती क्रमांक", "ನೋಂದಣಿ ಸಂಖ್ಯೆ", or similar IDs.
- "establishment_address" MUST map to the postal address / "आस्थापनेचा पत्ता".
"""
    elif doc_type == "MOA / AOA":
        coercion_rules = """\n### COERCION RULES FOR MOA / AOA:
- "company_name" MUST be the exact legal name of the company being incorporated.
- "cin" MUST be the 21-character alphanumeric Corporate Identity Number if present.
- "state_of_registration" MUST be the state where the registered office is situated (e.g., "Gujarat", "Haryana", "Telangana").
- "authorized_share_capital" MUST be the total numerical authorized share capital amount (e.g., "25000000", "100000").
- "registered_office_address" MUST be the actual physical street address / postal address. ABSOLUTELY DO NOT extract jurisdiction clauses like "within the jurisdiction of Registrar of Companies...". If Clause II only mentions the State, YOU MUST SCAN THE REST OF THE DOCUMENT (like the subscriber details table at the end) to find the physical street address. If no physical street address is found, return null.
"""
    elif doc_type == "CIBIL Report":
        coercion_rules = """\n### COERCION RULES FOR CIBIL REPORT:
- "cibil_score" MUST be the explicit 3-digit numerical credit score (e.g., 773, 750, 820). DO NOT extract descriptive words.
- "applicant_name" MUST be the actual human name listed under "Personal Information".
"""
    elif doc_type == "Rent Agreement":
        coercion_rules = """\n### COERCION RULES FOR RENT AGREEMENT:
- "monthly_rent" MUST be ONLY the final numerical amount (e.g., "71500", "8000"). DO NOT include words like "Rupees", "Rs.", or spelled-out numbers.
- "lease_start_date" and "lease_end_date" MUST be extracted as formal dates. Do not extract phrases like "after expiry".
"""
    elif doc_type == "PAN Card":
        coercion_rules = """\n### COERCION RULES FOR PAN CARD:
- "pan_number" MUST be the 10-character alphanumeric ID (e.g., ABCDE1234F).
- "full_name" MUST be the primary cardholder's name. It is usually the first full name appearing on the card.
- "father_name" MUST be the name explicitly listed under "Father's Name" or "पिता का नाम". Do not mix this up with the primary name.
- "taxpayer_status" MUST be inferred from the 4th letter of the PAN (P=Individual, C=Company, H=HUF, F=Firm, A=AOP, T=Trust).
"""
    elif doc_type == "Tax Audit Report":
        coercion_rules = """\n### COERCION RULES FOR TAX AUDIT REPORT:
- "entity_name" MUST be the exact name of the business or individual being audited (the Assessee).
- "assessment_year" MUST be extracted in a standard format (e.g., "2023-2024"). Do not confuse this with the Financial Year.
- "auditor_name" MUST be the human name of the Chartered Accountant (CA) conducting the audit.
- "ca_membership_number" MUST be the numerical membership or registration number of the CA.
"""
    
    return f"""You are a strict Document Intelligence Engine designed to parse text data and isolate highly specific parameters from documents.
Assume the current evaluation year is 2026. The user has identified this document as a: {doc_type}.

### PROPERTIES TO ISOLATE:
Extract ONLY the following properties: {target_fields}.
(CRITICAL: If parsing financial tables, numbers may be separated by multiple spaces or written in Lakhs/Crores. Extract the explicit values).{coercion_rules}
CRITICAL JSON RULES:
1. You MUST output STRICTLY valid JSON.
2. ALL property keys MUST be enclosed in double quotes ("). Do not use single quotes.
3. ALL date fields MUST be formatted strictly as 'DD/MM/YYYY' (e.g., 31/12/2026, WITH slashes '/' between numbers).
4. Output exactly this format and nothing else:
{{
  "document_type": "{doc_type}",
  "summary": "Provide a descriptive 2-line processing note. Include validation alerts.",
  "fields": [
    {{
      "label": "exact_parameter_key_name",
      "value": "extracted raw value or null",
      "type": "one of: name|date|amount|contact|id|location|category|other"
    }}
  ]
}}
Return ONLY the raw JSON object."""

def isolate_fs_components(pages_text):
    full_sample = " ".join(pages_text[:15]).lower()
    if not any(k in full_sample for k in ["profit and loss", "profit & loss", "balance sheet", "cash flow"]):
        return None 
        
    logger.info("[ROUTER] 📊 Financial Statement Detected! Isolating core pages...")
    best_pnl = ""; max_pnl = 0
    best_bs = ""; max_bs = 0
    best_cf = ""; max_cf = 0

    for page in pages_text:
        lower = page.lower()
        pnl_score = 0
        if any(k in lower for k in ["statement of profit and loss", "profit and loss account", "statement of profit & loss", "profit & loss statement"]): pnl_score += 8
        if any(k in lower for k in ["revenue from operations", "total income"]): pnl_score += 4
        if any(k in lower for k in ["profit before tax", "profit for the year", "net profit"]): pnl_score += 3
        
        bs_score = 0
        if "balance sheet" in lower and not any(k in lower for k in ["profit and loss", "profit & loss"]): bs_score += 8
        if "equity and liabilities" in lower: bs_score += 5
        if "shareholders' funds" in lower or "total assets" in lower: bs_score += 3
        
        cf_score = 0
        if any(k in lower for k in ["cash flow statement", "statement of cash flows"]): cf_score += 8
        if "operating activities" in lower: cf_score += 4

        if pnl_score > max_pnl and pnl_score >= 6: max_pnl = pnl_score; best_pnl = page
        if bs_score > max_bs and bs_score >= 6: max_bs = bs_score; best_bs = page
        if cf_score > max_cf and cf_score >= 6: max_cf = cf_score; best_cf = page

    stitched = "--- EXTRACTED FINANCIAL COMPONENTS ---\n\n"
    if best_pnl: stitched += "--- PROFIT & LOSS STATEMENT ---\n" + best_pnl + "\n\n"
    if best_bs: stitched += "--- BALANCE SHEET ---\n" + best_bs + "\n\n"
    if best_cf: stitched += "--- CASH FLOW STATEMENT ---\n" + best_cf + "\n\n"
    
    return stitched if (best_pnl or best_bs or best_cf) else None

def process_doc_digitization(file_bytes, original_filename):
    headers = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}
    
    if original_filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'a', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(original_filename, file_bytes)
        file_bytes = zip_buffer.getvalue()
        target_filename = "document.zip"
    else:
        target_filename = original_filename

    init_res = requests.post(SARVAM_DOC_ENDPOINT, headers=headers, json={"job_parameters": {"output_format": "md"}})
    if not init_res.ok: raise Exception(f"Init Error: {init_res.text}")
    job_id = init_res.json()["job_id"]

    up_res = requests.post(f"{SARVAM_DOC_ENDPOINT}/upload-files", headers=headers, json={"job_id": job_id, "files": [target_filename]})
    if not up_res.ok: raise Exception(f"Upload Link Error: {up_res.text}")
    upload_url = up_res.json()["upload_urls"][target_filename]["file_url"]

    upload_headers = {"Content-Type": "application/octet-stream", "x-ms-blob-type": "BlockBlob"}
    upload_req = requests.put(upload_url, data=file_bytes, headers=upload_headers)
    if not upload_req.ok: raise Exception(f"Azure Upload Error: {upload_req.text}")

    start_res = requests.post(f"{SARVAM_DOC_ENDPOINT}/{job_id}/start", headers=headers)
    if not start_res.ok: raise Exception(f"Sarvam Start Error: {start_res.text}")

    timeout_counter = 0
    while True:
        time.sleep(3)
        timeout_counter += 3
        if timeout_counter > 60: raise Exception("Digitization polling exceeded 60 seconds.")

        stat_res = requests.get(f"{SARVAM_DOC_ENDPOINT}/{job_id}/status", headers=headers)
        if not stat_res.ok: raise Exception(f"Status Request Error: {stat_res.text}")
        
        stat_data = stat_res.json()
        state = stat_data["job_state"]

        if state == "Completed": break
        elif state in ["Failed", "PartiallyCompleted"]: raise Exception(f"Job failed: {stat_data.get('error_message', 'Unknown error')}")

    down_res = requests.post(f"{SARVAM_DOC_ENDPOINT}/{job_id}/download-files", headers=headers)
    if not down_res.ok: raise Exception(f"Download Link Error: {down_res.text}")
    download_url = list(down_res.json()["download_urls"].values())[0]["file_url"]

    zip_resp = requests.get(download_url)
    if not zip_resp.ok: raise Exception(f"ZIP Download Error: {zip_resp.text}")

    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        for name in zf.namelist():
            if name.endswith('.md'):
                return zf.read(name).decode('utf-8')
                
    raise Exception("No markdown file found in output.")

def classify_document_via_llm(file_bytes, filename):
    ext = filename.split('.')[-1].lower()
    combined_text = ""
    
    try:
        if ext == 'pdf':
            pdf_stream = io.BytesIO(file_bytes)
            reader = PdfReader(pdf_stream)
            num_pages = len(reader.pages)
            
            if num_pages > 0:
                extracted_text = reader.pages[0].extract_text()
                combined_text += (extracted_text if extracted_text else "") + "\n"
            if num_pages > 1:
                extracted_text_2 = reader.pages[1].extract_text()
                combined_text += (extracted_text_2 if extracted_text_2 else "") + "\n"
            
            if len(combined_text.strip()) < 50:
                logger.info("[CLASSIFIER] Scanned PDF. Running local OCR on Page 1 & 2...")
                images = convert_from_bytes(file_bytes, first_page=1, last_page=min(num_pages, 2), dpi=150)
                for img in images:
                    combined_text += pytesseract.image_to_string(img) + "\n"
                    
        elif ext in ['png', 'jpg', 'jpeg']:
            import cv2
            import numpy as np
            nparr = np.frombuffer(file_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            combined_text = pytesseract.image_to_string(gray)
        else:
            combined_text = file_bytes.decode('utf-8', errors='ignore')

        if not combined_text or not combined_text.strip():
            logger.warning("[CLASSIFIER] Warning: No text could be extracted.")
            return "General Document"

        logger.info("[CLASSIFIER] Sending Pages 1-2 to Sarvam Chat for AI Classification...")
        allowed_categories = ", ".join(STATUTORY_DOC_TYPES + list(DOCUMENT_SCHEMAS.keys()))
        
        classification_prompt = f"""You are an expert document classifier. Read the text from the first two pages of a document.
        Classify it into EXACTLY ONE of the following categories: 
        {allowed_categories}.
        
        ### STRICT CLASSIFICATION RULES (EVALUATE IN THIS EXACT ORDER):
        1. TAX AUDIT REPORT: If text contains "Form No. 3CB", "Form No. 3CA", "Form No. 3CD", "Tax Audit", or "under section 44AB of the Income-tax Act" -> "Tax Audit Report".
        2. MOA / AOA: If text contains "MEMORANDUM OF ASSOCIATION", "ARTICLES OF ASSOCIATION", "SPICe MOA", "e-Memorandum of Association" -> "MOA / AOA". (CRITICAL: If the document contains BOTH a Certificate of Incorporation AND a Memorandum/Articles of Association, you MUST classify it strictly as "MOA / AOA").
        3. INCORPORATION CERTIFICATE / COI: If the document is a 1-page certificate containing "Certificate of Incorporation", "Form No. INC-11", "Central Registration Centre" AND does NOT contain MOA/AOA keywords -> "Incorporation Certificate / COI".
        4. RENT AGREEMENT: If text contains "Rent Agreement", "Rental Agreement", "Lease Deed", "Leave and License", or "Deed of Lease" -> "Rent Agreement". DO NOT classify as a PAN card just because ID numbers are mentioned.
        5. CIBIL REPORT: If text contains "CIBIL", "TransUnion", "Credit Score", "Equifax", "Experian", or "Consumer Information Report" -> "CIBIL Report".
        6. PROPERTY TAX RECEIPT: If text contains "Property Tax", "Holding Tax", "Municipal Corporation", "Nagar Nigam", "Assessment-Collection", "Tax Assessment", or "Property Details" -> "Property Tax Receipt".
        7. PAN CARD: If text contains "Income Tax Department", "INCOME TAX", "Permanent Account Number", OR "GOVT. OF INDIA" -> "PAN Card".
        8. AADHAAR CARD: If text contains "Unique Identification Authority", "Aadhaar" -> "Aadhaar Card".
        9. VOTER ID: If text contains "Election Commission" OR "EPIC" -> "Voter ID".
        10. PASSPORT: If text contains "Republic of India", "Passport", or the MRZ code "P<IND" -> "Passport".
        11. FORM 16: If text contains "Certificate under section 203" OR "Form No. 16" -> "Form 16".
        12. GST RETURN (GSTR): If text contains "GSTR-1", "GSTR-3B", "Return" -> "GST Return (GSTR)".
        13. UDYAM CERTIFICATE: If text contains "Udyam Registration", "UDYAM", "Uog Aadhaar" -> "Udyam Certificate".
        14. GST/BUSINESS CERTIFICATE: If text contains "GST Registration", "GSTIN" -> "GST/Business Certificate".
        15. SHOP & ESTABLISHMENT CERTIFICATE: If text contains "Shops & Establishment Act", "Registration Certificate of Establishment", "दुकाने व आस्थापना" -> "Shop & Establishment Certificate".
        16. UTILITY BILL: If text is an invoice billing a customer for Gas, Water, or Electricity -> "Utility Bill / Invoice".
        17. SALARY SLIP: If text contains "Salary Slip", "Payslip", "Net Pay" -> "Salary Slip".
        18. BANK STATEMENT: If text contains "Bank Statement", "IFSC", and a ledger of transactions -> "Bank Statement".
        
        CRITICAL: Reply with ONLY the exact category name as a plain string. Do not include any other text or reasoning.
        
        Document Text:
        {combined_text[:3500]}"""

        payload = {
            "model": "sarvam-30b",
            "max_tokens": 50,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "CRITICAL: Keep your <think> reasoning extremely brief. You MUST output the final classification category exactly as it appears in the list."},
                {"role": "user", "content": classification_prompt}
            ]
        }
        
        headers = {"Content-Type": "application/json", "api-subscription-key": SARVAM_API_KEY}
        try:
            response = requests.post(SARVAM_CHAT_ENDPOINT, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            response_json = response.json()
            content = response_json.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            if not content:
                return "General Document"

            identified_category = content.strip()
            identified_category = re.sub(r'<think>.*?(</think>|$)', '', identified_category, flags=re.DOTALL).strip()
            identified_category = identified_category.replace('"', '').replace("'", '').split('\n')[-1].strip()
            
            all_cats = STATUTORY_DOC_TYPES + list(DOCUMENT_SCHEMAS.keys())
            for cat in all_cats:
                if cat.lower() in identified_category.lower():
                    logger.info(f"[CLASSIFIER] AI Classification Result: {cat}")
                    return cat
                
            logger.info(f"[CLASSIFIER] Fallback to General Document. Raw LLM output: {identified_category}")
            return "General Document"
        except Exception as e:
            logger.error(f"[CLASSIFIER] AI Classification Error: {e}")
            return "General Document"
            
    except Exception as e:
        logger.error(f"[CLASSIFIER] Error: {e}")
        return "General Document"

def process_pdf_smart_router(file_bytes, original_filename, doc_type):
    pdf_stream = io.BytesIO(file_bytes)
    
    if doc_type == "Financial Statement":
        logger.info("[ROUTER] 📊 Financial Statement Target! Using high-fidelity tabular extraction...")
        try:
            pages_text = []
            with pdfplumber.open(pdf_stream) as pdf:
                max_extract_pages = min(len(pdf.pages), 80)
                for i in range(max_extract_pages):
                    text = pdf.pages[i].extract_text(layout=True)
                    pages_text.append(text if text else "")
            
            total_extracted_chars = sum(len(p.strip()) for p in pages_text)
            if total_extracted_chars > 500:
                fs_extracted_text = isolate_fs_components(pages_text)
                return fs_extracted_text if fs_extracted_text else "\n".join(pages_text[:10])
        except Exception as e:
            logger.error(f"[ROUTER] pdfplumber fallback error: {e}")
            pdf_stream.seek(0)

    reader = PdfReader(pdf_stream)
    num_pages = len(reader.pages)
    pages_text = []
    
    slice_limit = 20 if doc_type in ITERATIVE_DOCS else 12
    actual_limit = min(num_pages, slice_limit)
    
    for i in range(actual_limit):
        page_content = reader.pages[i].extract_text()
        pages_text.append(page_content if page_content else "")
        
    total_extracted_chars = sum(len(p.strip()) for p in pages_text)
    
    if total_extracted_chars > 500:
        logger.info("[ROUTER] Natively extractable non-statutory PDF detected. Routing directly to LLM.")
        return "\n".join(pages_text)
        
    if actual_limit > 10:
        logger.info(f"[ROUTER] Scanned document exceeds 10 pages. Bypassing Sarvam API -> Local OCR.")
        try:
            images = convert_from_bytes(file_bytes, dpi=150, first_page=1, last_page=actual_limit)
            ocr_texts = []
            for idx, img in enumerate(images):
                logger.info(f"[ROUTER] Running local Tesseract OCR on page {idx+1}/{actual_limit}...")
                ocr_texts.append(pytesseract.image_to_string(img))
            return "\n".join(ocr_texts)
        except Exception as ocr_err:
            logger.error(f"[ROUTER] Local OCR failed: {ocr_err}")
            return ""

    logger.info(f"[ROUTER] Scanned document (<=10 pages) -> Sending to Sarvam Digitization API.")
    writer = PdfWriter()
    for i in range(actual_limit): writer.add_page(reader.pages[i])
    sliced_stream = io.BytesIO()
    writer.write(sliced_stream)
    sliced_bytes = sliced_stream.getvalue()

    try:
        return process_doc_digitization(sliced_bytes, "sliced_" + original_filename)
    except Exception as e:
        if "Insufficient credits" in str(e):
            return ""
        raise e

def iterative_local_extraction(doc_type, file_bytes):
    pdf_stream = io.BytesIO(file_bytes)
    reader = PdfReader(pdf_stream)
    num_pages = len(reader.pages)
    
    slice_limit = 5 if doc_type in ["Aadhaar Card", "Passport", "Voter ID", "Driving License"] else 12
    actual_limit = min(num_pages, slice_limit)
    chunk_size = 5
    combined_text = ""
    best_data = None
    max_found = -1
    
    for start_page in range(0, actual_limit, chunk_size):
        end_page = min(start_page + chunk_size, actual_limit)
        logger.info(f"[ROUTER] 🏛️ Iterative Statutory OCR: Pages {start_page+1} to {end_page}...")
        
        for i in range(start_page, end_page):
            try:
                page_content = reader.pages[i].extract_text()
                if page_content: combined_text += page_content + "\n"
            except: pass
            
        combined_text += "\n--- VISUAL OCR LAYER ---\n"
        
        try:
            images = convert_from_bytes(file_bytes, dpi=150, first_page=start_page+1, last_page=end_page)
            for img in images:
                combined_text += pytesseract.image_to_string(img) + "\n"
        except Exception as e:
            logger.error(f"[ROUTER] Local OCR failed on chunk: {e}")
            
        local_data = extract_locally(doc_type, combined_text)
        
        found_count = sum(1 for f in local_data['fields'] if f['value'])
        if found_count > max_found:
            max_found = found_count
            best_data = local_data
            
        if found_count == len(local_data['fields']):
            logger.info(f"[ROUTER] All statutory fields found! Stopping early at page {end_page}.")
            break
            
    return best_data if best_data else extract_locally(doc_type, combined_text)

def get_statutory_extraction(doc_type, file_bytes, ext):
    if ext == 'pdf':
        return iterative_local_extraction(doc_type, file_bytes)
    else:
        try:
            import cv2
            import numpy as np
            nparr = np.frombuffer(file_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            document_text = pytesseract.image_to_string(gray)
            return extract_locally(doc_type, document_text)
        except Exception as e:
            logger.error(f"[LOCAL] Image Statutory Extraction failed: {e}")
            return extract_locally(doc_type, "")

# =====================================================================
# 🔥 ITERATIVE EXTRACTION ENGINE FOR MULTI-PAGE AGREEMENTS
# =====================================================================
def iterative_llm_extraction(doc_type, document_text):
    target_fields = [k.strip().replace('"', '') for k in DOCUMENT_SCHEMAS[doc_type].split(',')]
    current_state = {field: None for field in target_fields}
    
    compressed_text = re.sub(r' {4,}', '    ', document_text)
    chunk_size = 18000 
    chunks = [compressed_text[i:i+chunk_size] for i in range(0, len(compressed_text), chunk_size)]
    
    for idx, chunk in enumerate(chunks):
        missing_fields = [k for k, v in current_state.items() if not v or str(v).strip().lower() in ['null', 'none', 'n/a', 'not discovered', '']]
        
        if not missing_fields:
            logger.info(f"[ITERATIVE ENGINE] All fields found for {doc_type}. Stopping early at chunk {idx}.")
            break
            
        logger.info(f"[ITERATIVE ENGINE] Processing chunk {idx+1}/{len(chunks)} for {doc_type}. Hunting for: {missing_fields}")
        
        missing_schema_str = ", ".join([f'"{f}"' for f in missing_fields])
        
        sys_prompt = generate_system_prompt(doc_type)

        payload = {
            "model": "sarvam-30b",
            "max_tokens": 2000,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": sys_prompt + f"\n\nCRITICAL: You are only extracting these specific missing properties: {missing_schema_str}. Keep your <think> reasoning extremely brief."},
                {"role": "user", "content": f"Extract the target parameters from this document chunk:\n\n{chunk}"}
            ]
        }

        headers = {"Content-Type": "application/json", "api-subscription-key": SARVAM_API_KEY}
        try:
            res = requests.post(SARVAM_CHAT_ENDPOINT, json=payload, headers=headers, timeout=60)
            res.raise_for_status()
            
            content_str = res.json()['choices'][0]['message']['content']
            
            content_str = re.sub(r'<think>.*?</think>', '', content_str, flags=re.DOTALL).strip()
            cleaned_json_str = content_str.replace('```json', '').replace('```JSON', '').replace('```', '').strip()
            
            start_idx = cleaned_json_str.find('{')
            end_idx = cleaned_json_str.rfind('}')
            if start_idx != -1 and end_idx != -1:
                cleaned_json_str = cleaned_json_str[start_idx:end_idx+1]
                
            chunk_data = json.loads(cleaned_json_str)
            
            for item in chunk_data.get("fields", []):
                lbl = str(item.get("label", "")).strip().lower().replace(" ", "_")
                val = item.get("value")
                
                matched_key = next((k for k in current_state.keys() if k.lower() == lbl), None)
                
                if matched_key and val and str(val).strip().lower() not in ['null', 'none', 'n/a', 'not discovered', '']:
                    
                    if matched_key == 'registered_office_address':
                        val_lower = str(val).lower()
                        if 'jurisdiction' in val_lower or 'registrar' in val_lower or len(val_lower) < 15:
                            continue
                            
                    current_state[matched_key] = val
                    
        except Exception as e:
            logger.error(f"[ITERATIVE ENGINE] Error on chunk {idx+1}: {e}")
            continue 

    final_fields = []
    for k in target_fields:
        ftype = "other"
        if any(t in k for t in ["name", "landlord", "tenant", "partner", "holder", "company"]): ftype = "name"
        elif "date" in k or "year" in k: ftype = "date"
        elif "amount" in k or "rent" in k or "ratio" in k or "capital" in k: ftype = "amount"
        elif "number" in k or "ifsc" in k or "cin" in k: ftype = "id"
        elif "address" in k or "place" in k or "bank" in k or "state" in k: ftype = "location"
        
        val = current_state[k]
        
        if ftype == "date" and val:
            val = _format_date_with_slashes(val)
            
        final_fields.append({
            "label": k,
            "value": val,
            "type": ftype
        })
        
    missing_left = [k for k, v in current_state.items() if not v]
    if missing_left:
        summary = f"Processed {len(chunks)} document chunks. Some fields remain undiscovered: {', '.join(missing_left)}"
    else:
        summary = f"Iterative AI extraction complete. All targets successfully found."
        
    return {
        "document_type": doc_type,
        "summary": summary,
        "fields": final_fields
    }

# =====================================================================
# FLASK ROUTES
# =====================================================================

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/classify', methods=['POST'])
def classify_file():
    if 'document' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files['document']
    file_bytes = file.read()
    
    doc_type = classify_document_via_llm(file_bytes, file.filename)
    return jsonify({"document_type": doc_type})

@app.route('/api/extract', methods=['POST'])
def extract_data():
    if 'document' not in request.files:
        return jsonify({"error": "No file uploaded in the request"}), 400

    file = request.files['document']
    filename = file.filename
    ext = filename.split('.')[-1].lower()
    
    doc_type = request.form.get('document_type', 'General Document')
    file_bytes = file.read()
    
    try:
        if doc_type in STATUTORY_DOC_TYPES and doc_type != "PAN Card":
            local_data = get_statutory_extraction(doc_type, file_bytes, ext)
            return jsonify({"choices": [{"message": {"content": json.dumps(local_data)}}]})

        if ext in ['txt', 'html', 'json']:
            document_text = file_bytes.decode('utf-8', errors='ignore')
        elif ext == 'pdf':
            document_text = process_pdf_smart_router(file_bytes, filename, doc_type)
        elif ext in ['png', 'jpg', 'jpeg']:
            try:
                document_text = process_doc_digitization(file_bytes, filename)
            except Exception as e:
                if "Insufficient credits" in str(e):
                    import cv2
                    import numpy as np
                    logger.warning("[BACKEND] ⚠️ Sarvam Out of Credits! Running preprocessed local OCR...")
                    nparr = np.frombuffer(file_bytes, np.uint8)
                    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                    custom_config = r'--oem 3 --psm 6'
                    document_text = pytesseract.image_to_string(thresh, config=custom_config)
                else:
                    raise e
        else:
            return jsonify({"error": "Unsupported file format"}), 400
    except Exception as e:
        logger.error(f"[BACKEND] Digitization error: {e}")
        return jsonify({"error": f"Digitization error: {str(e)}"}), 500
            
    if doc_type in ITERATIVE_DOCS:
        logger.info(f"[BACKEND] 🚀 Triggering Iterative Multi-Page Extraction for {doc_type}...")
        try:
            iterative_data = iterative_llm_extraction(doc_type, document_text)
            mock_response = {"choices": [{"message": {"content": json.dumps(iterative_data)}}]}
            return jsonify(mock_response)
        except Exception as e:
            logger.error(f"[BACKEND] Iterative Engine Failed: {e}")
            return jsonify({"error": f"Extraction anomaly: {str(e)}"}), 500

    compressed_text = re.sub(r' {4,}', '    ', document_text)
    truncated_text = compressed_text[:35000]
    
    user_prompt = f"Extract the target parameters for the attached document payload:\n\n{truncated_text}"
    dynamic_system_prompt = generate_system_prompt(doc_type)

    sarvam_payload = {
        "model": "sarvam-30b",
        "max_tokens": 2000,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": dynamic_system_prompt + "\n\nCRITICAL: Keep your <think> reasoning extremely brief (under 3 sentences)."},
            {"role": "user", "content": user_prompt}
        ]
    }

    headers = {"Content-Type": "application/json", "api-subscription-key": SARVAM_API_KEY}

    try:
        session = requests.Session()
        session.trust_env = False
        response = session.post(SARVAM_CHAT_ENDPOINT, json=sarvam_payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        resp_json = response.json()
        content_str = resp_json['choices'][0]['message']['content']
        
        content_str = re.sub(r'<think>.*?</think>', '', content_str, flags=re.DOTALL).strip()
        cleaned_json_str = content_str.replace('```json', '').replace('```JSON', '').replace('```', '').strip()
        
        start_idx = cleaned_json_str.find('{')
        end_idx = cleaned_json_str.rfind('}')
        if start_idx != -1 and end_idx != -1:
            cleaned_json_str = cleaned_json_str[start_idx:end_idx+1]
            
        try:
            extracted_json = json.loads(cleaned_json_str)
            
            fixed_fields = []
            for field in extracted_json.get("fields", []):
                lbl = str(field.get("label", "")).strip().lower().replace(" ", "_")
                val = field.get("value")
                if field.get("type") == "date" and val:
                    val = _format_date_with_slashes(val)
                fixed_fields.append({"label": lbl, "value": val, "type": field.get("type", "other")})
            extracted_json["fields"] = fixed_fields
            
            cleaned_json_str = json.dumps(extracted_json)
        except Exception as e:
            pass
            
        resp_json['choices'][0]['message']['content'] = cleaned_json_str
        return jsonify(resp_json)

    except Exception as e:
        return jsonify({"error": f"API transmission anomaly: {str(e)}"}), 500

@app.route('/api/extract_financial', methods=['POST'])
def extract_financial_data():
    if 'document' not in request.files:
        return jsonify({"error": "No PDF document uploaded"}), 400
    file = request.files['document']
    
    if 'coordinates' not in request.form:
        return jsonify({"error": "Missing coordinate JSON"}), 400
    
    try:
        coords_json = json.loads(request.form['coordinates'])
    except Exception as e:
        return jsonify({"error": f"Invalid coordinate JSON: {str(e)}"}), 400

    temp_pdf_path = f"temp_{file.filename}"
    file.save(temp_pdf_path)

    try:
        engine = FinancialStatementEngine(pdf_path=temp_pdf_path, coordinates_json=coords_json)
        structured_financial_data = engine.process()
        os.remove(temp_pdf_path)
        return jsonify(structured_financial_data)
    except Exception as e:
        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)
        return jsonify({"error": f"Financial extraction failed: {str(e)}"}), 500

@app.route('/api/verify_application', methods=['POST'])
def verify_application_route():
    if 'document' not in request.files: return jsonify({"error": "No document"}), 400
    file = request.files['document']
    doc_type = request.form.get('document_type', 'General Document')
    filename = file.filename
    ext = filename.split('.')[-1].lower()
    file_bytes = file.read()
    
    try:
        jarvis_data = json.loads(request.form.get('jarvis_data', '{}'))
    except Exception:
        jarvis_data = {}

    try:
        if doc_type in STATUTORY_DOC_TYPES and doc_type != "PAN Card":
            extracted_json = get_statutory_extraction(doc_type, file_bytes, ext)
            flat_doc_data = {item['label']: item['value'] for item in extracted_json.get('fields', [])}
            
        else:
            if ext in ['txt', 'html', 'json']: document_text = file_bytes.decode('utf-8', errors='ignore')
            elif ext == 'pdf': document_text = process_pdf_smart_router(file_bytes, filename, doc_type)
            elif ext in ['png', 'jpg', 'jpeg']:
                try: document_text = process_doc_digitization(file_bytes, filename)
                except Exception: document_text = ""
            else: return jsonify({"error": "Unsupported format"}), 400

            document_text = re.sub(r' {4,}', '    ', document_text)[:35000]

            if doc_type in ITERATIVE_DOCS:
                extracted_json = iterative_llm_extraction(doc_type, document_text)
                flat_doc_data = {item['label']: item['value'] for item in extracted_json.get('fields', [])}
                
            else:
                payload = {
                    "model": "sarvam-30b", 
                    "max_tokens": 2000, 
                    "temperature": 0.1,
                    "messages": [
                        {"role": "system", "content": generate_system_prompt(doc_type)},
                        {"role": "user", "content": f"Extract parameters from this document text:\n\n{document_text}"}
                    ]
                }
                res = requests.post(SARVAM_CHAT_ENDPOINT, json=payload, headers={"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"})
                res.raise_for_status()
                
                content_str = res.json()['choices'][0]['message']['content']
                content_str = re.sub(r'<think>.*?</think>', '', content_str, flags=re.DOTALL).strip()
                cleaned_json_str = content_str.replace('```json', '').replace('```JSON', '').replace('```', '').strip()
                
                start_idx = cleaned_json_str.find('{')
                end_idx = cleaned_json_str.rfind('}')
                if start_idx != -1 and end_idx != -1:
                    cleaned_json_str = cleaned_json_str[start_idx:end_idx+1]
                
                try:
                    extracted_json = json.loads(cleaned_json_str)
                    
                    fixed_fields = []
                    for field in extracted_json.get("fields", []):
                        lbl = str(field.get("label", "")).strip().lower().replace(" ", "_")
                        val = field.get("value")
                        if field.get("type") == "date" and val:
                            val = _format_date_with_slashes(val)
                        fixed_fields.append({"label": lbl, "value": val, "type": field.get("type", "other")})
                    extracted_json["fields"] = fixed_fields
                            
                except:
                    extracted_json = {"fields": []}
                    
                flat_doc_data = {item['label']: item['value'] for item in extracted_json.get('fields', [])}

        compliance_report = rule_engine.evaluate(jarvis_data, flat_doc_data)

        return jsonify({
            "status": "success",
            "extracted_data": extracted_json, 
            "compliance_report": compliance_report
        })

    except Exception as e:
        return jsonify({"error": f"Verification failed: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
