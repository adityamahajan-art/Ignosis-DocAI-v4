"""
LOCAL EXTRACTION ENGINE — Statutory Document Regex/Heuristic Processor
Extracts structured fields from statutory documents locally (no API traffic).
"""
import re
import json

# =====================================================================
# STRICT SCHEMA DEFINITIONS (all keys must appear in output)
# =====================================================================
STATUTORY_SCHEMAS = {
    "PAN Card": ["pan_number", "full_name", "father_name", "date_of_birth", "taxpayer_status"],
    "Aadhaar Card": ["aadhaar_number", "full_name", "date_of_birth", "gender", "address_raw", "pin_code"],
    "Form 60": ["full_name", "aadhaar_number", "pan_number", "mobile_number", "residential_address", "office_address"],
    "Driving License": ["id_number", "full_name", "date_of_birth", "expiry_date", "gender"],
    "Voter ID": ["epic_no", "full_name", "relative_name", "date_of_birth", "gender"],
    "Passport": ["passport_number", "full_name", "date_of_birth", "expiry_date", "nationality", "gender"],
    "GST/Business Certificate": ["enterprise_name", "gstin", "constitution_of_business", "date_of_incorporation_or_registration", "principal_place_of_business"],
    "Udyam Certificate": ["enterprise_name", "udyam_registration_number", "uan_number", "type_of_organisation", "date_of_registration", "enterprise_address"],
    "Incorporation Certificate / COI": ["company_name", "cin_number", "date_of_incorporation", "registered_office_address"],
    "MOA / AOA": ["company_name", "cin", "state_of_registration", "authorized_share_capital", "registered_office_address"],
    "Pension Order (PPO)": ["pensioner_name", "ppo_number", "pensioner_address", "date_of_issue"],
    "Form 16": ["employee_name", "employer_name", "pan_of_employee", "pan_of_employer", "assessment_year", "total_income_paid"],
    "Income Tax Return (ITR)": ["pan_number", "full_name", "assessment_year", "total_income", "total_tax_payable"],
    "GST Return (GSTR)": ["gstin", "legal_name", "return_period", "total_taxable_value", "total_tax_paid"],
}

STATUTORY_DOC_TYPES = list(STATUTORY_SCHEMAS.keys())

# =====================================================================
# FIELD TYPE CLASSIFIER
# =====================================================================
def _get_field_type(key):
    if any(t in key for t in ["name", "employer", "applicant", "pensioner", "company", "enterprise", "establishment"]):
        return "name"
    if any(t in key for t in ["date", "year", "period", "validity"]):
        return "date"
    if any(t in key for t in ["amount", "income", "tax", "salary", "capital", "value", "paid"]):
        return "amount"
    if any(t in key for t in ["mobile", "phone", "contact"]):
        return "contact"
    if any(t in key for t in ["number", "pan", "aadhaar", "gstin", "cin", "epic", "passport", "uan", "ppo", "ifsc", "id_number", "score"]):
        return "id"
    if any(t in key for t in ["address", "place", "pin_code", "state", "location", "office"]):
        return "location"
    return "other"

# =====================================================================
# COMMON REGEX PATTERNS
# =====================================================================
_DATE_PATTERN = r'(\b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\b)'
_PAN_PATTERN = r'\b([A-Z]{5}\d{4}[A-Z])'
_AADHAAR_PATTERN = r'(\b\d{4}\s\d{4}\s\d{4}\b|\b\d{12}\b)'
_PINCODE_PATTERN = r'(\b[1-9]\d{5}\b)'
_GSTIN_PATTERN = r'(\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b)'
_CIN_PATTERN = r'(\b[A-Z]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b)'
_MOBILE_PATTERN = r'(\b[6-9]\d{9}\b)'
_DL_PATTERN = r'(\b[A-Z]{2}\d{2}\s?\d{4}\s?\d{7}\b|\b[A-Z]{2}-\d{13}\b)'
_EPIC_PATTERN = r'(\b[A-Z]{3}\d{7}\b|\b[A-Z]{3}/\d{7}\b)'
_PASSPORT_PATTERN = r'(\b[A-Z]\d{7}\b)'
_UDYAM_PATTERN = r'(\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b)'
_PPO_PATTERN = r'(\b[A-Z0-9/\-]{5,20}\b)'
_AY_PATTERN = r'(\b\d{4}\s*[-–]\s*\d{2,4}\b)'

# =====================================================================
# ROBUST HEURISTIC EXTRACTOR HELPERS
# =====================================================================

def _first_match(text, pattern, flags=re.IGNORECASE):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None

def _extract_value_next_line_or_after(text, label_pattern):
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if re.search(label_pattern, line, re.IGNORECASE):
            m = re.search(label_pattern + r'\s*[:\-]?\s*([^\n]+)', line, re.IGNORECASE)
            if m and len(m.group(1).strip()) > 1:
                val = m.group(1).strip()
                if not any(lbl in val.lower() for lbl in ["name", "address", "date", "number", "email", "phone"]):
                    return val
            for j in range(i + 1, min(i + 4, len(lines))):
                candidate = lines[j].strip()
                if candidate and len(candidate) > 1:
                    if re.search(r'^[A-Z\d\(\)]+\s*[:\-]', candidate) or "![" in candidate or "]" in candidate:
                        continue
                    return candidate
    return None

def _find_name_after(text, label_pattern):
    name = _extract_value_next_line_or_after(text, label_pattern)
    if name:
        name = re.sub(r'\s{2,}', ' ', name)
        if len(name) > 2 and len(name) < 120 and re.search(r'[A-Za-z]', name):
            return name
    return None

def _grab_address_block(text, label_pattern):
    lines = text.split('\n')
    address_lines = []
    for i, line in enumerate(lines):
        if re.search(label_pattern, line, re.IGNORECASE):
            m = re.search(label_pattern + r'\s*[:\-]?\s*(.*)', line, re.IGNORECASE)
            if m and m.group(1).strip():
                address_lines.append(m.group(1).strip())
            for j in range(i + 1, min(i + 7, len(lines))):
                next_line = lines[j].strip()
                if not next_line: continue
                if any(k in next_line.lower() for k in ["help@", "www.", "uidai", "aadhaar", "signature", "photo", "issued:"]):
                    break
                if re.search(r'\b\d{4}\s\d{4}\s\d{4}\b', next_line): break
                address_lines.append(next_line)
            break
    if address_lines:
        full_addr = ", ".join(address_lines)
        full_addr = re.sub(r'\s+', ' ', full_addr)
        full_addr = re.sub(r',\s*,', ',', full_addr)
        return full_addr.strip(', ').strip()
    return None

def _find_amount_near_label(text, label_pattern):
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if len(line) > 80: continue
        if re.search(label_pattern, line, re.IGNORECASE):
            for j in range(i, min(i + 7, len(lines))):
                if len(lines[j]) > 80: continue
                m = re.search(r'\b\d{1,3}(?:,\d{2,3})*(?:,\d{3})\b', lines[j])
                if m: return m.group(0).strip()
            for j in range(i, min(i + 7, len(lines))):
                if len(lines[j]) > 80: continue
                m = re.search(r'\b\d{3,10}\b', lines[j])
                if m: return m.group(0).strip()
            for j in range(i, min(i + 7, len(lines))):
                if len(lines[j]) > 80: continue
                m = re.search(r'\b\d+\b', lines[j])
                if m: return m.group(0).strip()
    return None

def _convert_spelled_date(date_str):
    if not date_str: return None
    s = date_str.lower().replace('-', ' ')
    mm = None
    months = {'january': '01', 'february': '02', 'march': '03', 'april': '04', 'may': '05', 'june': '06', 
              'july': '07', 'august': '08', 'september': '09', 'october': '10', 'november': '11', 'december': '12'}
    for m, v in months.items():
        if m in s:
            mm = v
            break
    dd = None
    days_map = {
        'thirty first': '31', 'thirtieth': '30', 'twenty ninth': '29', 'twenty eighth': '28', 'twenty seventh': '27', 
        'twenty sixth': '26', 'twenty fifth': '25', 'twenty fourth': '24', 'twenty third': '23', 'twenty second': '22', 
        'twenty first': '21', 'twentieth': '20', 'nineteenth': '19', 'eighteenth': '18', 'seventeenth': '17', 
        'sixteenth': '16', 'fifteenth': '15', 'fourteenth': '14', 'thirteenth': '13', 'twelfth': '12', 'eleventh': '11',
        'tenth': '10', 'ninth': '09', 'eighth': '08', 'seventh': '07', 'sixth': '06', 'fifth': '05', 'fourth': '04', 
        'third': '03', 'second': '02', 'first': '01'
    }
    for d_word, v in days_map.items():
        if d_word in s:
            dd = v
            break
    if not dd:
        m_dd = re.search(r'\b(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\b', s)
        if m_dd: dd = m_dd.group(1).zfill(2)
    yy = None
    if mm and "two thousand" in s:
        if "twenty six" in s: yy = "2026"
        elif "twenty five" in s: yy = "2025"
        elif "twenty four" in s: yy = "2024"
        elif "twenty three" in s: yy = "2023"
        elif "twenty two" in s: yy = "2022"
        elif "twenty one" in s: yy = "2021"
        elif "twenty" in s: yy = "2020" 
        elif "nineteen" in s: yy = "2019"
        elif "eighteen" in s: yy = "2018"
        elif "seventeen" in s: yy = "2017"
        elif "sixteen" in s: yy = "2016"
        elif "fifteen" in s: yy = "2015"
        elif "fourteen" in s: yy = "2014"
        elif "thirteen" in s: yy = "2013"
        elif "twelve" in s: yy = "2012"
        elif "eleven" in s: yy = "2011"
        elif "ten" in s: yy = "2010"
        elif "nine" in s: yy = "2009"
        elif "eight" in s: yy = "2008"
        elif "seven" in s: yy = "2007"
        elif "six" in s: yy = "2006"
        elif "five" in s: yy = "2005"
        elif "four" in s: yy = "2004"
        elif "three" in s: yy = "2003"
        elif "two" in s: yy = "2002"
        elif "one" in s: yy = "2001"
        else: yy = "2000"
    if not yy:
        m_yr = re.search(r'\b(19\d{2}|20\d{2})\b', s)
        if m_yr: yy = m_yr.group(1)
    if dd and mm and yy:
        return f"{dd}/{mm}/{yy}" 
    return date_str.strip()

def _get_taxpayer_status(pan):
    if pan and len(pan) >= 4:
        char = pan[3].upper()
        mapping = { 'P': 'Individual', 'C': 'Company', 'H': 'HUF', 'F': 'Firm', 'A': 'AOP', 'T': 'Trust', 'G': 'Government' }
        return mapping.get(char, 'Individual')
    return 'Individual'

def _get_constitution_from_name(name):
    if not name: return "Proprietorship"
    name_no_space = name.upper().replace(" ", "")
    if "PRIVATELIMITED" in name_no_space or "PVTLTD" in name_no_space: return "Private Limited Company"
    if "LIMITED" in name_no_space or "LTD" in name_no_space: return "Public Limited Company"
    if "LLP" in name_no_space or "PARTNERSHIP" in name_no_space: return "Partnership / LLP"
    return "Proprietorship"

# =====================================================================
# PER-DOCUMENT EXTRACTORS
# =====================================================================

def _extract_pan_card(text):
    pan = _first_match(text, _PAN_PATTERN)
    full_name = _find_name_after(text, r'(?:Name|नाम|নাম)')
    father_name = _find_name_after(text, r"(?:Father'?s?\s*Name|पिता\s*का\s*नाम)")
    if not full_name or not father_name:
        noise = ["income tax", "department", "permanent", "account", "number", "govt", "india", "signature", "photo", "card", "satyamev", "jayate", "सत्यमेव", "जयते", "विभाग", "आयकर", "भारत", "सरकार"]
        lines = text.split('\n')
        candidates = []
        for line in lines:
            cleaned = line.strip()
            if not cleaned or "![" in cleaned or any(c.isdigit() for c in cleaned): continue
            if any(kw in cleaned.lower() for kw in noise): continue
            words = [w for w in cleaned.split() if w.isalpha() and w.isupper()]
            if len(words) >= 2: candidates.append(" ".join(words))
        if not full_name and len(candidates) >= 1: full_name = candidates[0]
        if not father_name and len(candidates) >= 2: father_name = candidates[1]
    return {
        "pan_number": pan,
        "full_name": full_name,
        "father_name": father_name,
        "date_of_birth": _first_match(text, _DATE_PATTERN),
        "taxpayer_status": _get_taxpayer_status(pan),
    }

def _extract_aadhaar_card(text):
    full_name = None
    lines = text.split('\n')
    dob_idx = -1
    for i, line in enumerate(lines):
        if any(k in line.lower() for k in ["dob:", "date of birth:", "yob:", "जन्मतिथि:", "জন্ম"]):
            dob_idx = i; break
    if dob_idx != -1:
        for j in range(dob_idx - 1, max(-1, dob_idx - 4), -1):
            cleaned = lines[j].strip()
            if cleaned and "![" not in cleaned and sum(c.isdigit() for c in cleaned) <= 3:
                words = [w for w in cleaned.split() if w[0].isupper() or not w.islower()]
                if len(words) >= 2 and not any(k in cleaned.lower() for k in ["government", "india", "unique"]):
                    full_name = cleaned; break
    if not full_name: full_name = _find_name_after(text, r'(?:Name|नाम|নাম)')
    return {
        "aadhaar_number": _first_match(text, _AADHAAR_PATTERN),
        "full_name": full_name,
        "date_of_birth": _first_match(text, r'(?:DOB|Date\s*of\s*Birth|YOB)\s*[:\-]?\s*' + _DATE_PATTERN) or _first_match(text, _DATE_PATTERN),
        "gender": _first_match(text, r'\b(Male|Female|MALE|FEMALE|पुरुष|महिला|Transgender)\b'),
        "address_raw": _grab_address_block(text, r'(?:Address|पता|S/O|W/O|C/O|D/O)'),
        "pin_code": _first_match(text, _PINCODE_PATTERN),
    }

def _extract_form_60(text):
    return {
        "full_name": _find_name_after(text, r'(?:Name|Full\s*Name)'),
        "aadhaar_number": _first_match(text, _AADHAAR_PATTERN),
        "pan_number": _first_match(text, _PAN_PATTERN),
        "mobile_number": _first_match(text, _MOBILE_PATTERN),
        "residential_address": _grab_address_block(text, r'(?:Residential\s*Address|Address)'),
        "office_address": _grab_address_block(text, r'(?:Office\s*Address|Business\s*Address)'),
    }

def _extract_driving_license(text):
    return {
        "id_number": _first_match(text, _DL_PATTERN) or _first_match(text, r'(?:DL\s*No|License\s*No|Licence\s*No)\.?\s*[:\-]?\s*([A-Z0-9\-/]+)'),
        "full_name": _find_name_after(text, r'(?:Name|नाम|নাম)'),
        "date_of_birth": _first_match(text, r'(?:DOB|Date\s*of\s*Birth)\s*[:\-]?\s*' + _DATE_PATTERN) or _first_match(text, _DATE_PATTERN),
        "expiry_date": _first_match(text, r'(?:Valid\s*(?:Till|Upto)|Expiry)\s*[:\-]?\s*' + _DATE_PATTERN),
        "gender": _first_match(text, r'\b(Male|Female|MALE|FEMALE|M|F)\b'),
    }

def _extract_voter_id(text):
    return {
        "epic_no": _first_match(text, _EPIC_PATTERN) or _first_match(text, r'(?:EPIC|Voter\s*ID)\s*(?:No\.?)?\s*[:\-]?\s*([A-Z0-9/]+)'),
        "full_name": _find_name_after(text, r'(?:Name|Elector\'?s?\s*Name)'),
        "relative_name": _find_name_after(text, r"(?:Father'?s?\s*Name|Husband'?s?\s*Name|Mother'?s?\s*Name)"),
        "date_of_birth": _first_match(text, r'(?:DOB|Date\s*of\s*Birth|Age)\s*[:\-]?\s*' + _DATE_PATTERN) or _first_match(text, _DATE_PATTERN),
        "gender": _first_match(text, r'\b(Male|Female|MALE|FEMALE)\b'),
    }

def _extract_passport(text):
    extracted = {
        "passport_number": None,
        "full_name": None,
        "date_of_birth": None,
        "expiry_date": None,
        "nationality": "INDIAN",
        "gender": None
    }
    
    clean_text = text.replace(" ", "").replace("\n", "")
    mrz1 = re.search(r'(P<IND[A-Z<]+)', clean_text)
    mrz2 = re.search(r'([A-Z0-9<]{8,9}\dIND\d{6}\d[MF<]\d{6}.+)', clean_text)
    
    if mrz1 and mrz2:
        l1 = mrz1.group(1)
        l2 = mrz2.group(1)
        name_part = l1[5:].strip('<')
        parts = name_part.split('<<')
        if len(parts) == 2:
            extracted["full_name"] = f"{parts[1].replace('<', ' ').strip()} {parts[0].replace('<', ' ').strip()}".strip()
        else:
            extracted["full_name"] = name_part.replace('<', ' ').strip()
            
        extracted["passport_number"] = l2[0:9].replace('<', '').strip()
        dob_str = l2[13:19]
        if len(dob_str) == 6 and dob_str.isdigit():
            yy, mm, dd = dob_str[0:2], dob_str[2:4], dob_str[4:6]
            year = f"19{yy}" if int(yy) > 25 else f"20{yy}"
            extracted["date_of_birth"] = f"{dd}/{mm}/{year}"
            
        extracted["gender"] = "Male" if l2[20].upper() == 'M' else "Female" if l2[20].upper() == 'F' else l2[20].upper()
        
        exp_str = l2[21:27]
        if len(exp_str) == 6 and exp_str.isdigit():
            yy, mm, dd = exp_str[0:2], exp_str[2:4], exp_str[4:6]
            extracted["expiry_date"] = f"{dd}/{mm}/20{yy}"

    if not extracted["passport_number"]:
        extracted["passport_number"] = _first_match(text, r'(?:Passport\s*No)[^\w]*([A-Z][0-9]{7})') or _first_match(text, _PASSPORT_PATTERN)
        
    if not extracted["full_name"]:
        surname = _extract_value_next_line_or_after(text, r'(?:Surname|उपनाम)')
        given = _extract_value_next_line_or_after(text, r'(?:Given\s*Name|दिया\s*गया\s*नाम)')
        if surname and given:
            extracted["full_name"] = f"{given} {surname}".strip()
        else:
            extracted["full_name"] = _find_name_after(text, r'(?:Given\s*Name|Surname|Name)')
            
    if not extracted["date_of_birth"]:
        m = re.search(r'(?:Date\s*of\s*Birth|जन्म\s*तिथि|DOB)[^\d]*(\d{2}[/\-]\d{2}[/\-]\d{4})', text, re.IGNORECASE)
        if m: extracted["date_of_birth"] = m.group(1).replace('-', '/')
    
    if not extracted["expiry_date"]:
        m = re.search(r'(?:Date\s*of\s*Expiry|समाप्ति\s*की\s*तिथि|Expiry)[^\d]*(\d{2}[/\-]\d{2}[/\-]\d{4})', text, re.IGNORECASE)
        if m: extracted["expiry_date"] = m.group(1).replace('-', '/')
        
    if not extracted["gender"]:
        m = re.search(r'(?:Sex|लिंग)[^\w]*([M|F])\b', text, re.IGNORECASE)
        if m: extracted["gender"] = "Male" if m.group(1).upper() == 'M' else "Female"
        else: extracted["gender"] = _first_match(text, r'\b(Male|Female|MALE|FEMALE|M|F)\b')

    return extracted

def _extract_business_address_table_layout(text):
    lines = text.split('\n')
    addr_start = -1
    for i, line in enumerate(lines):
        if re.search(r'Flat/Door/Block|Flat/Door|Flat\s*/\s*Door', line, re.IGNORECASE):
            addr_start = i; break
    if addr_start != -1:
        address_lines = []
        for j in range(addr_start, min(addr_start + 10, len(lines))):
            next_line = lines[j].strip()
            if not next_line: continue
            if any(k in next_line.lower() for k in ["mobile", "email:", "website"]): break
            address_lines.append(next_line)
        full_addr = ", ".join(address_lines)
        full_addr = re.sub(r'Nameof\s*Premises/\s*Building|Premises/\s*Building', ' ', full_addr, flags=re.IGNORECASE)
        full_addr = re.sub(r'Village/Town|Road/Street/Lane|District', ', ', full_addr, flags=re.IGNORECASE)
        full_addr = re.sub(r'State\s+[A-Z\s]+District', ', ', full_addr, flags=re.IGNORECASE)
        full_addr = re.sub(r'Pin\b', ', Pin: ', full_addr, flags=re.IGNORECASE)
        full_addr = re.sub(r'Flat/Door/Block\s*', '', full_addr, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', re.sub(r',\s*,', ',', full_addr)).strip(', ').strip()
    return None

def _extract_gst_certificate(text):
    enterprise_name = _find_name_after(text, r'(?:Legal\s*Name|Trade\s*Name|Name\s*of\s*(?:the\s*)?Business)')
    gstin = _first_match(text, _GSTIN_PATTERN) or _first_match(text, _UDYAM_PATTERN)
    constitution = _extract_value_next_line_or_after(text, r'(?:Constitution\s*of\s*Business|Type\s*of\s*Enterprise)')
    if not constitution or constitution in ["MAJORACTIVITY", "SERVICES"]:
        constitution = _get_constitution_from_name(enterprise_name)
    address = _extract_business_address_table_layout(text) or _grab_address_block(text, r'(?:Principal\s*Place\s*of\s*Business|Registered\s*Address)')
    return {
        "enterprise_name": enterprise_name,
        "gstin": gstin,
        "constitution_of_business": constitution,
        "date_of_incorporation_or_registration": _first_match(text, r'(?:Date\s*of\s*(?:Registration|Liability))\s*[:\-]?\s*' + _DATE_PATTERN) or _first_match(text, _DATE_PATTERN),
        "principal_place_of_business": address,
    }

def _extract_udyam_certificate(text):
    enterprise_name = _find_name_after(text, r'(?:Name\s*of\s*Enterprise|Enterprise\s*Name)')
    udyam_no = _first_match(text, _UDYAM_PATTERN)
    org_type = _extract_value_next_line_or_after(text, r'(?:Type\s*of\s*(?:Organisation|Enterprise))')
    if not org_type or org_type in ["MAJORACTIVITY", "SERVICES"]:
        org_type = _get_constitution_from_name(enterprise_name)
    address = _extract_business_address_table_layout(text) or _grab_address_block(text, r'(?:Address|Plant\s*Location|Enterprise\s*Address)')
    return {
        "enterprise_name": enterprise_name,
        "udyam_registration_number": udyam_no,
        "uan_number": udyam_no,
        "type_of_organisation": org_type,
        "date_of_registration": _first_match(text, r'(?:Date\s*of\s*Registration)\s*[:\-]?\s*' + _DATE_PATTERN) or _first_match(text, _DATE_PATTERN),
        "enterprise_address": address,
    }

def _extract_coi(text):
    extracted = {
        "company_name": None,
        "cin_number": None,
        "date_of_incorporation": None,
        "registered_office_address": None
    }
    flat_text = re.sub(r'\s+', ' ', text)
    m_cin = re.search(r'\b([LU]\s*\d{5}\s*[A-Z]{2}\s*\d{4}\s*[A-Z]{3}\s*\d{6})\b', flat_text, re.IGNORECASE)
    if m_cin:
        extracted["cin_number"] = re.sub(r'\s+', '', m_cin.group(1)).upper()
    else:
        m_cin_fallback = re.search(r'(?:CIN|Corporate\s*Identity\s*Number).*?([LU][A-Z0-9]{20})\b', flat_text, re.IGNORECASE)
        if m_cin_fallback: extracted["cin_number"] = m_cin_fallback.group(1).upper()

    m_cert = re.search(r'certify\s+that\s+(.*?)\s+is\s+incorporated\s+(?:on\s+this|on)\s+(.*?)\s+under', flat_text, re.IGNORECASE)
    if m_cert:
        extracted["company_name"] = m_cert.group(1).strip()
        raw_date = m_cert.group(2).strip()
        extracted["date_of_incorporation"] = _convert_spelled_date(raw_date)
    else:
        extracted["company_name"] = _find_name_after(text, r'(?:Name\s*of\s*(?:the\s*)?Company|Company\s*Name)')
        m_date = re.search(r'incorporated\s+(?:on\s+this|on)\s+(.*?)\s+under', flat_text, re.IGNORECASE)
        if m_date:
            extracted["date_of_incorporation"] = _convert_spelled_date(m_date.group(1).strip())
        else:
            extracted["date_of_incorporation"] = _first_match(text, _DATE_PATTERN)
            
    if extracted["company_name"]:
        extracted["company_name"] = re.sub(r'(?i)\s+is\s+incorporated.*', '', extracted["company_name"]).strip()

    m_addr = re.search(r'Mailing\s+Address\s+as\s+per\s+record.*?office\s*[:\-;,]?\s*(.*?)(?:Disclaimer|Given\s+under|DS\s+MINISTRY|Signature|$)', flat_text, re.IGNORECASE)
    if m_addr:
        addr = m_addr.group(1).strip()
        addr = re.sub(r'(?i)[\*\+]\s*s?\s*issued\s+by.*', '', addr).strip()
        extracted["registered_office_address"] = addr
        if not extracted["company_name"] and ',' in addr:
            extracted["company_name"] = addr.split(',')[0].strip()
    else:
        addr = _grab_address_block(text, r'(?:Registered\s*Office|Address|situated\s*at)')
        if addr:
            addr = re.split(r'(?i)(Disclaimer|Given under)', addr)[0].strip()
            extracted["registered_office_address"] = addr
    return extracted

def _extract_moa_aoa(text):
    return {
        "company_name": _find_name_after(text, r'(?:Company\s*Name|Name\s*of\s*(?:the\s*)?Company)'),
        "cin": _first_match(text, r'\b([L|U|l|u][A-Za-z0-9]{20})\b'),
        "state_of_registration": _first_match(text, r'(?:State|Registered\s*(?:in|at))\s*[:\-]?\s*([A-Za-z\s]+)'),
        "authorized_share_capital": _find_amount_near_label(text, r'(?:Authorized\s*(?:Share\s*)?Capital|Authorised)'),
        "registered_office_address": _grab_address_block(text, r'(?:Registered\s*Office|Address)'),
    }

# 🔥 COMPLETELY REWRITTEN PPO EXTRACTOR
def _extract_ppo(text):
    extracted = {
        "pensioner_name": None,
        "ppo_number": None,
        "pensioner_address": None,
        "date_of_issue": None
    }
    flat_text = re.sub(r'\s+', ' ', text)

    # 1. PPO NUMBER: Force it to contain at least 1 digit to prevent capturing "Print" or "Order"
    m_ppo = re.search(r'(?:P\.?P\.?O\.?|Pension\s*Payment\s*Order)\s*(?:No\.?|Number)?[\s\:\-]*([A-Z0-9/\-]+)', flat_text, re.IGNORECASE)
    if m_ppo:
        val = m_ppo.group(1).strip()
        # Ensure the extracted value is NOT just a formatted date, but does contain digits
        if not re.match(r'^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$', val) and any(c.isdigit() for c in val):
            extracted["ppo_number"] = val
            
    # Fallback: Look for a massive 10-15 digit standalone ID block (common in central PPOs)
    if not extracted["ppo_number"]:
        m_ppo_fallback = re.search(r'\b(\d{10,15})\b', text)
        if m_ppo_fallback:
            extracted["ppo_number"] = m_ppo_fallback.group(1)

    # 2. PENSIONER NAME: Anchor and cut off at standard next-field headers
    m_name = re.search(r'(?:Name\s*of\s*(?:the\s*)?(?:Pensioner|Govt\.?\s*servant|recipient\s*of\s*family\s*pension)|Pensioner\s*Name|Name)[\s\:\-]*([A-Za-z\.\s]{4,60})', flat_text, re.IGNORECASE)
    if m_name:
        name_val = m_name.group(1).strip()
        # Cut off immediately if we hit a standard table header
        name_val = re.split(r'(?i)(Gender|Date|DOB|Relationship|Post|Office)', name_val)[0].strip()
        # Clear out any trailing digits that snuck in
        name_val = re.sub(r'\s+\d.*$', '', name_val).strip()
        extracted["pensioner_name"] = name_val
    else:
        name_val = _find_name_after(text, r'(?:Name\s*of\s*(?:the\s*)?Pensioner|Pensioner\s*Name|Name)')
        if name_val:
            extracted["pensioner_name"] = re.split(r'(?i)(Gender|Date|DOB|Relationship|Post|Office)', name_val)[0].strip()

    # 3. ADDRESS: Multi-anchor trap with list artifact cleaner
    m_addr = re.search(r'(?:Residential|Permanent|Present)\s*Address\s*[\:\-]?\s*(.*?)(?:Personal\s*marks|Date\s*of|Signature|Photograph|Branch\s*Name|IFSC|Mobile|Phone|Rule|Amount|Section\s*2)', flat_text, re.IGNORECASE)
    if m_addr:
        addr_val = m_addr.group(1).strip()
        # Strip trailing list numbers (e.g. "5. ")
        addr_val = re.sub(r'^\d[\.\)]\s*', '', addr_val).strip()
        addr_val = re.sub(r'\s*\d[\.\)]\s*$', '', addr_val).strip()
        extracted["pensioner_address"] = addr_val
    else:
        addr = _grab_address_block(text, r'(?:Address|Residential\s*Address|Permanent\s*Address)')
        if addr:
            addr = re.split(r'(?i)(Date|Class|Personal|Signature|Branch|IFSC|Section)', addr)[0].strip()
            # Strip trailing list numbers
            extracted["pensioner_address"] = re.sub(r'\s*\d[\.\)]\s*$', '', addr).strip()

    # 4. DATE OF ISSUE
    m_date = re.search(r'(?:Print\s*Date|Date\s*of\s*Issue|Issue\s*Date)[\s\:\-]*' + _DATE_PATTERN, flat_text, re.IGNORECASE)
    if m_date:
        extracted["date_of_issue"] = m_date.group(1).strip()
    else:
        extracted["date_of_issue"] = _first_match(text, _DATE_PATTERN)

    return extracted

def _extract_form16(text):
    lines = text.split('\n')
    employer = None
    for i, line in enumerate(lines):
        if re.search(r'Name\s*(?:and\s*address)?\s*of\s*(?:the\s*)?Employer', line, re.IGNORECASE):
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j].strip()
                if cand and not any(k in cand.lower() for k in ["name", "address", "pan", "tan"]):
                    employer = cand; break
            break
            
    employee = None
    for i, line in enumerate(lines):
        if re.search(r'Employee', line, re.IGNORECASE) and any(k in line.lower() for k in ["name", "address"]):
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j].strip()
                if cand and cand != employer and not any(k in cand.lower() for k in ["name", "address", "pan"]):
                    employee = cand; break
            break
            
    if employee:
        employee = re.sub(r'^(?:Rail\s+)?Bhawan\s*', '', employee, flags=re.IGNORECASE)
        if ',' in employee: employee = employee.split(',')[0].strip()

    pan_employee = _first_match(text, r'PAN\s*of\s*(?:the\s*)?Employee[^A-Z]*' + _PAN_PATTERN)
    pan_employer = _first_match(text, r'PAN\s*of\s*(?:the\s*)?(?:Deductor|Employer)[^A-Z]*' + _PAN_PATTERN)

    if not pan_employee or not pan_employer:
        unique_pans = list(dict.fromkeys(re.findall(_PAN_PATTERN, text)))
        for p in unique_pans:
            if len(p) >= 4:
                if p[3].upper() == 'P' and not pan_employee: pan_employee = p
                elif not pan_employer: pan_employer = p

    return {
        "employee_name": employee,
        "employer_name": employer,
        "pan_of_employee": pan_employee,
        "pan_of_employer": pan_employer,
        "assessment_year": _first_match(text, r'(?:Assessment\s*Year)\s*[\:\-]?\s*' + _AY_PATTERN) or _first_match(text, _AY_PATTERN),
        "total_income_paid": _find_amount_near_label(text, r'(?:1\.\s*Gross\s*Salary|Gross\s*Salary|Total\s*Gross\s*Salary)'),
    }

def _extract_itr(text):
    first_name = _extract_value_next_line_or_after(text, r'First\s*Name')
    middle_name = _extract_value_next_line_or_after(text, r'Middle\s*Name')
    last_name = _extract_value_next_line_or_after(text, r'Last\s*Name')
    
    full_name = None
    if first_name:
        parts = [first_name]
        if middle_name: parts.append(middle_name)
        if last_name: parts.append(last_name)
        full_name = " ".join(parts)
    else:
        full_name = _find_name_after(text, r'(?:Name|Full\s*Name|Name\s*of\s*(?:the\s*)?Assessee)')

    return {
        "pan_number": _first_match(text, _PAN_PATTERN),
        "full_name": full_name,
        "assessment_year": _first_match(text, _AY_PATTERN) or _first_match(text, r'Assessment\s*Year\s*\n*(\d{4}\s*[-–]\s*\d{2,4})'),
        "total_income": _find_amount_near_label(text, r'(?:Total\s*Income|Gross\s*Total\s*Income|Gross\s*Salary)'),
        "total_tax_payable": _find_amount_near_label(text, r'(?:Amount\s*payable|Total\s*Tax\s*(?:Payable|Liability|Due))') or "0",
    }

def _extract_gstr(text):
    return {
        "gstin": _first_match(text, _GSTIN_PATTERN),
        "legal_name": _find_name_after(text, r'(?:Legal\s*Name|Trade\s*Name|Name)'),
        "return_period": _extract_value_next_line_or_after(text, r'(?:Return\s*Period|Tax\s*Period|Period)'),
        "total_taxable_value": _find_amount_near_label(text, r'(?:Total\s*Taxable\s*Value|Taxable\s*Value)'),
        "total_tax_paid": _find_amount_near_label(text, r'(?:Total\s*Tax\s*(?:Paid|Payable|Liability)|Total\s*Tax)'),
    }


# =====================================================================
# MASTER DISPATCHER
# =====================================================================
_EXTRACTORS = {
    "PAN Card": _extract_pan_card,
    "Aadhaar Card": _extract_aadhaar_card,
    "Form 60": _extract_form_60,
    "Driving License": _extract_driving_license,
    "Voter ID": _extract_voter_id,
    "Passport": _extract_passport,
    "GST/Business Certificate": _extract_gst_certificate,
    "Udyam Certificate": _extract_udyam_certificate,
    "Incorporation Certificate / COI": _extract_coi,
    "MOA / AOA": _extract_moa_aoa,
    "Pension Order (PPO)": _extract_ppo,
    "Form 16": _extract_form16,
    "Income Tax Return (ITR)": _extract_itr,
    "GST Return (GSTR)": _extract_gstr,
}


def extract_locally(doc_type: str, text: str) -> dict:
    schema_keys = STATUTORY_SCHEMAS.get(doc_type, [])
    extractor_fn = _EXTRACTORS.get(doc_type)

    raw_data = {}
    if extractor_fn:
        try:
            raw_data = extractor_fn(text)
        except Exception as e:
            print(f"[LOCAL_EXTRACTOR] Extraction error for {doc_type}: {e}")

    fields = []
    found_count = 0
    for key in schema_keys:
        val = raw_data.get(key)
        if val is not None and str(val).strip():
            found_count += 1
        fields.append({
            "label": key,
            "value": val if (val is not None and str(val).strip()) else None,
            "type": _get_field_type(key),
        })

    summary = f"Local extraction engine processed this {doc_type}. {found_count}/{len(schema_keys)} fields were successfully extracted via regex/heuristics."
    if found_count < len(schema_keys):
        summary += f" {len(schema_keys) - found_count} field(s) could not be identified — review the source document for missing data."

    return {
        "document_type": doc_type,
        "summary": summary,
        "fields": fields,
    }