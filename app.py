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

SARVAM_API_KEY = os.environ.get('SARVAM_API_KEY', 'sk_gjwhvmqe_d2vFC2FA245pX2gkPTXLZYhO')
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
    "General Document": '"entity_name", "document_date", "primary_amount", "key_reference_number"'
}

ITERATIVE_DOCS = ["Rent Agreement", "Loan Agreement", "Partnership Deed"]

def generate_system_prompt(doc_type):
    target_fields = DOCUMENT_SCHEMAS.get(doc_type, DOCUMENT_SCHEMAS["General Document"])
    
    coercion_rules = ""
    if doc_type == "Property Tax Receipt":
        coercion_rules = """
### COERCION RULES FOR PROPERTY TAX RECEIPT:
- "receipt_number" MUST extract the Receipt No, Transaction ID, Acknowledgement No, or Challan No.
- "tax_amount" MUST extract the final numerical amount paid/payable.
- "financial_year" MUST extract the specific billing period or year.
- "owner_name" MUST extract the human or entity name next to Owner, Assessee Name, or "Received From". Do not include the label itself.
"""
    elif doc_type == "Shop & Establishment Certificate":
        coercion_rules = """
### COERCION RULES FOR SHOP & ESTABLISHMENT CERTIFICATE:
- IMPORTANT: These certificates are often heavily localized in regional languages (e.g., Marathi, Kannada, Hindi). Translate and map contextually.
- "establishment_name" MUST map to "Name of the Establishment", "आस्थापनेचे नाव", "ಸಂಸ್ಥೆಯ ಹೆಸರು", or similar headings.
- "employer_name" MUST map to "Name of the Employer", "मालकाचे नाव", "ಮಾಲೀಕರ ಹೆಸರು", or Proprietor/Owner.
- "registration_number" MUST map to Registration No, "पावती क्रमांक", "ನೋಂದಣಿ ಸಂಖ್ಯೆ", or similar IDs.
- "establishment_address" MUST map to the postal address / "आस्थापनेचा पत्ता".
"""
    
    return f"""You are a strict Document Intelligence Engine designed to parse text data and isolate highly specific parameters from documents.
Assume the current evaluation year is 2026. The user has identified this document as a: {doc_type}.

### PROPERTIES TO ISOLATE:
Extract ONLY the following properties: {target_fields}.
(CRITICAL: If parsing financial tables, numbers may be separated by multiple spaces or written in Lakhs/Crores. Extract the explicit values).
{coercion_rules}
CRITICAL JSON RULES:
1. You MUST output STRICTLY valid JSON.
2. ALL property keys MUST be enclosed in double quotes ("). Do not use single quotes.
3. Output exactly this format and nothing else:
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
    page_1_text = ""
    
    try:
        if ext == 'pdf':
            pdf_stream = io.BytesIO(file_bytes)
            reader = PdfReader(pdf_stream)
            if len(reader.pages) > 0:
                extracted_text = reader.pages[0].extract_text()
                page_1_text = extracted_text if extracted_text else ""
            
            if len(page_1_text.strip()) < 50:
                logger.info("[CLASSIFIER] Scanned PDF. Running local OCR on Page 1...")
                images = convert_from_bytes(file_bytes, first_page=1, last_page=1, dpi=150)
                if images:
                    page_1_text = pytesseract.image_to_string(images[0])
                    
        elif ext in ['png', 'jpg', 'jpeg']:
            import cv2
            import numpy as np
            nparr = np.frombuffer(file_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            page_1_text = pytesseract.image_to_string(gray)
        else:
            page_1_text = file_bytes.decode('utf-8', errors='ignore')[:2000]

        if not page_1_text or not page_1_text.strip():
            logger.warning("[CLASSIFIER] Warning: No text could be extracted.")
            return "General Document"

        logger.info("[CLASSIFIER] Sending Page 1 to Sarvam Chat for AI Classification...")
        allowed_categories = ", ".join(STATUTORY_DOC_TYPES + list(DOCUMENT_SCHEMAS.keys()))
        
        classification_prompt = f"""You are an expert document classifier. Read the text from page 1 of a document.
        Classify it into EXACTLY ONE of the following categories: 
        {allowed_categories}.
        
        ### STRICT CLASSIFICATION RULES (EVALUATE IN THIS EXACT ORDER):
        1. RENT AGREEMENT: If text contains "Rent Agreement", "Lease Deed", "Leave and License" -> "Rent Agreement".
        2. PAN CARD: If text contains "Income Tax Department" AND "Permanent Account Number" -> "PAN Card".
        3. AADHAAR CARD: If text contains "Unique Identification Authority", "Aadhaar" -> "Aadhaar Card".
        4. VOTER ID: If text contains "Election Commission" OR "EPIC" -> "Voter ID".
        5. PASSPORT: If text contains "Republic of India", "Passport", or the MRZ code "P<IND" -> "Passport".
        6. FORM 16: If text contains "Certificate under section 203" OR "Form No. 16" -> "Form 16".
        7. GST RETURN (GSTR): If text contains "GSTR-1", "GSTR-3B", "Return" -> "GST Return (GSTR)".
        8. UDYAM CERTIFICATE: If text contains "Udyam Registration", "UDYAM", "Udyog Aadhaar" -> "Udyam Certificate".
        9. GST/BUSINESS CERTIFICATE: If text contains "GST Registration", "GSTIN" -> "GST/Business Certificate".
        10. PROPERTY TAX RECEIPT: If text contains "Property Tax", "Holding Tax", "Municipal Corporation", "Nagar Nigam", or "Tax Assessment" -> "Property Tax Receipt".
        11. SHOP & ESTABLISHMENT CERTIFICATE: If text contains "Shops & Establishment Act", "Registration Certificate of Establishment", "दुकाने व आस्थापना" -> "Shop & Establishment Certificate".
        12. UTILITY BILL: If text is an invoice billing a customer for Gas, Water, or Electricity -> "Utility Bill / Invoice".
        13. SALARY SLIP: If text contains "Salary Slip", "Payslip", "Net Pay" -> "Salary Slip".
        14. BANK STATEMENT: If text contains "Bank Statement", "IFSC", and a ledger of transactions -> "Bank Statement".
        
        CRITICAL: Reply with ONLY the exact category name as a plain string. Do not include any other text or reasoning.
        
        Document Text:
        {page_1_text[:2500]}"""

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
            
        return "General Document"
            
    except Exception as e:
        logger.error(f"[CLASSIFIER] AI Classification Error: {e}")
        return "General Document"

def process_pdf_smart_router(file_bytes, original_filename, doc_type):
    pdf_stream = io.BytesIO(file_bytes)
    
    if doc_type == "Financial Statement":
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
            pdf_stream.seek(0)

    reader = PdfReader(pdf_stream)
    num_pages = len(reader.pages)
    pages_text = []
    
    max_extract_pages = min(num_pages, 80)
    for i in range(max_extract_pages):
        page_content = reader.pages[i].extract_text()
        pages_text.append(page_content if page_content else "")
        
    total_extracted_chars = sum(len(p.strip()) for p in pages_text)
    
    if total_extracted_chars > 500:
        if doc_type == "Aadhaar Card":
            return "\n".join(pages_text[:5])
        elif doc_type in ["Income Tax Return (ITR)", "GST Return (GSTR)", "Form 16", "Financial Statement", "MOA / AOA"]:
            return "\n".join(pages_text[:12])
        # 🔥 FIX: Read full text for long iterative documents
        elif doc_type in ITERATIVE_DOCS:
            return "\n".join(pages_text)
        return "\n".join(pages_text[:3])
        
    # 🔥 FIX: Expanded slicing limits to ensure long agreements are fully digitized
    if doc_type in ITERATIVE_DOCS:
        slice_limit = 20
    elif doc_type in ["Financial Statement", "Income Tax Return (ITR)", "GST Return (GSTR)", "Form 16", "MOA / AOA"]:
        slice_limit = 12
    elif doc_type == "Aadhaar Card":
        slice_limit = 5
    else:
        slice_limit = 3
        
    if num_pages > slice_limit:
        writer = PdfWriter()
        for i in range(slice_limit): writer.add_page(reader.pages[i])
        sliced_stream = io.BytesIO()
        writer.write(sliced_stream)
        file_bytes = sliced_stream.getvalue()
        target_filename = "sliced_" + original_filename
    else:
        target_filename = original_filename
            
    if doc_type in STATUTORY_DOC_TYPES:
        try:
            images = convert_from_bytes(file_bytes, dpi=150)
            ocr_texts = []
            for idx, img in enumerate(images):
                page_txt = pytesseract.image_to_string(img)
                ocr_texts.append(page_txt if page_txt else "")
            return "\n".join(ocr_texts)
        except Exception as ocr_err:
            return ""

    try:
        return process_doc_digitization(file_bytes, target_filename)
    except Exception as e:
        if "Insufficient credits" in str(e):
            return ""
        raise e

# =====================================================================
# 🔥 NEW ITERATIVE EXTRACTION ENGINE FOR MULTI-PAGE AGREEMENTS
# =====================================================================
def iterative_llm_extraction(doc_type, document_text):
    """
    Chunks a long document into ~5 page blocks. Asks the LLM to extract fields.
    If fields are found, they are removed from the next prompt. The loop aborts
    early as soon as all requirements are fulfilled to save API costs.
    """
    target_fields = [k.strip().replace('"', '') for k in DOCUMENT_SCHEMAS[doc_type].split(',')]
    current_state = {field: None for field in target_fields}
    
    # Compress massive whitespaces to save LLM tokens
    compressed_text = re.sub(r' {4,}', '    ', document_text)
    
    # Approx 5 pages of text per chunk (18,000 characters)
    chunk_size = 18000 
    chunks = [compressed_text[i:i+chunk_size] for i in range(0, len(compressed_text), chunk_size)]
    
    for idx, chunk in enumerate(chunks):
        # Identify what we still need to find
        missing_fields = [k for k, v in current_state.items() if not v or str(v).strip().lower() in ['null', 'none', 'n/a', 'not discovered', '']]
        
        # If we found everything, stop wasting API credits and break the loop!
        if not missing_fields:
            logger.info(f"[ITERATIVE ENGINE] All fields found for {doc_type}. Stopping early at chunk {idx}.")
            break
            
        logger.info(f"[ITERATIVE ENGINE] Processing chunk {idx+1}/{len(chunks)} for {doc_type}. Hunting for: {missing_fields}")
        
        missing_schema_str = ", ".join([f'"{f}"' for f in missing_fields])
        
        sys_prompt = f"""You are a strict Document Intelligence Engine.
Assume the evaluation year is 2026. Document Type: {doc_type}.

### PROPERTIES TO ISOLATE:
Extract ONLY these specific missing properties: {missing_schema_str}.

CRITICAL JSON RULES:
1. You MUST output STRICTLY valid JSON.
2. ALL property keys MUST be enclosed in double quotes.
3. Output exactly this format:
{{
  "document_type": "{doc_type}",
  "summary": "Extraction from part {idx+1}",
  "fields": [
    {{
      "label": "exact_parameter_key_name",
      "value": "extracted raw value or null",
      "type": "other"
    }}
  ]
}}
Return ONLY the raw JSON object."""

        payload = {
            "model": "sarvam-30b",
            "max_tokens": 2000,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": sys_prompt + "\n\nCRITICAL: Keep your <think> reasoning extremely brief."},
                {"role": "user", "content": f"Extract the target parameters from this document chunk:\n\n{chunk}"}
            ]
        }

        headers = {"Content-Type": "application/json", "api-subscription-key": SARVAM_API_KEY}
        try:
            res = requests.post(SARVAM_CHAT_ENDPOINT, json=payload, headers=headers, timeout=60)
            res.raise_for_status()
            
            content_str = res.json()['choices'][0]['message']['content']
            cleaned_json_str = content_str.replace('```json', '').replace('```JSON', '').replace('```', '').strip()
            chunk_data = json.loads(cleaned_json_str)
            
            # Map discovered values into the running state memory
            for item in chunk_data.get("fields", []):
                lbl = item.get("label")
                val = item.get("value")
                if lbl in current_state and val and str(val).strip().lower() not in ['null', 'none', 'n/a', 'not discovered', '']:
                    current_state[lbl] = val
                    
        except Exception as e:
            logger.error(f"[ITERATIVE ENGINE] Error on chunk {idx+1}: {e}")
            continue 

    # Reconstruct the final Frontend Payload
    final_fields = []
    for k in target_fields:
        ftype = "other"
        if any(t in k for t in ["name", "landlord", "tenant", "partner", "holder"]): ftype = "name"
        elif "date" in k: ftype = "date"
        elif "amount" in k or "rent" in k or "ratio" in k: ftype = "amount"
        elif "number" in k or "ifsc" in k: ftype = "id"
        elif "address" in k or "place" in k or "bank" in k: ftype = "location"
        
        final_fields.append({
            "label": k,
            "value": current_state[k],
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
        if ext in ['txt', 'html', 'json']:
            document_text = file_bytes.decode('utf-8', errors='ignore')
        elif ext == 'pdf':
            document_text = process_pdf_smart_router(file_bytes, filename, doc_type)
        elif ext in ['png', 'jpg', 'jpeg']:
            if doc_type in STATUTORY_DOC_TYPES:
                logger.info(f"[ROUTER] 🏛️ Local OCR triggered for Statutory Image: {doc_type}")
                try:
                    import cv2
                    import numpy as np
                    nparr = np.frombuffer(file_bytes, np.uint8)
                    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                    document_text = pytesseract.image_to_string(gray)
                except Exception as img_err:
                    document_text = ""
            else:
                try:
                    document_text = process_doc_digitization(file_bytes, filename)
                except Exception as e:
                    if "Insufficient credits" in str(e):
                        import cv2
                        import numpy as np
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
        return jsonify({"error": f"Digitization error: {str(e)}"}), 500

    if doc_type in STATUTORY_DOC_TYPES:
        try:
            local_data = extract_locally(doc_type, document_text)
            mock_response = {
                "choices": [{"message": {"content": json.dumps(local_data)}}]
            }
            return jsonify(mock_response)
        except Exception as e:
            mock_response = {"choices": [{"message": {"content": json.dumps({"document_type": doc_type, "summary": "Failed", "fields": []})}}]}
            return jsonify(mock_response)
            
    # 🔥 TRIGGER ITERATIVE ENGINE FOR TARGET MULTI-PAGE DOCUMENTS
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
        cleaned_json_str = content_str.replace('```json', '').replace('```JSON', '').replace('```', '').strip()
        
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
        if ext in ['txt', 'html', 'json']: document_text = file_bytes.decode('utf-8', errors='ignore')
        elif ext == 'pdf': document_text = process_pdf_smart_router(file_bytes, filename, doc_type)
        elif ext in ['png', 'jpg', 'jpeg']:
            if doc_type in STATUTORY_DOC_TYPES:
                try:
                    import cv2, numpy as np
                    img = cv2.imdecode(np.frombuffer(file_bytes, np.uint8), cv2.IMREAD_COLOR)
                    document_text = pytesseract.image_to_string(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
                except Exception: document_text = ""
            else:
                try: document_text = process_doc_digitization(file_bytes, filename)
                except Exception: document_text = ""
        else: return jsonify({"error": "Unsupported format"}), 400

        document_text = re.sub(r' {4,}', '    ', document_text)[:35000]

        if doc_type in STATUTORY_DOC_TYPES:
            extracted_json = extract_locally(doc_type, document_text)
            flat_doc_data = {item['label']: item['value'] for item in extracted_json.get('fields', [])}
            
        elif doc_type in ITERATIVE_DOCS:
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
            cleaned_json_str = content_str.replace('```json', '').replace('```JSON', '').replace('```', '').strip()
            
            try:
                extracted_json = json.loads(cleaned_json_str)
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
