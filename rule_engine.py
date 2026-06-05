import os
import pandas as pd
from pydantic import BaseModel
from thefuzz import fuzz

# =====================================================================
# DATA MAPPING DICTIONARY
# Maps Rulebook 'Field_Name' -> Our Extracted Schema JSON Keys
# =====================================================================
FIELD_MAPPING = {
    "Applicant Name": "full_name",
    "Applicant's  PAN Number": "pan_number",
    "GSTIN Number": "gstin",
    "Constitution Of Buisness": "constitution_of_business",
    "Date of incorporation": "date_of_registration",
    "Legal Name / Buisness Name": "enterprise_name",
    "Udyam Registration Number": "udyam_registration_number",
    "Type of organisation": "type_of_organisation",
    "UAN Number": "uan_number",
    "CIN Number": "cin_number",
    "Date of regestration": "date_of_registration"
}

class LoginRule(BaseModel):
    rule_number: str
    entity_name: str
    field_name: str
    match_criteria: str
    status: str

class RuleEngine:
    def __init__(self, excel_path: str):
        self.excel_path = excel_path
        self.rules = self._load_rules()

    def _load_rules(self) -> list[LoginRule]:
        if not os.path.exists(self.excel_path):
            print(f"[RULE ENGINE] Warning: Rulebook not found at {self.excel_path}")
            return []
        try:
            df = pd.read_excel(self.excel_path)  # Removed sheet_name to prevent pandas version conflicts
        except Exception:
            try:
                csv_path = self.excel_path + " - Sheet1.csv" if not self.excel_path.endswith('.csv') else self.excel_path
                df = pd.read_csv(csv_path)
            except Exception as e:
                print(f"[RULE ENGINE] Error loading rulebook: {e}")
                return []
        if 'Entity_Names' in df.columns:
            df['Entity_Names'] = df['Entity_Names'].ffill()
        active_rules = []
        for _, row in df.iterrows():
            if str(row.get('Status')).strip().lower() == 'active':
                active_rules.append(LoginRule(
                    rule_number=str(row.get('Rule_Number', '')),
                    entity_name=str(row.get('Entity_Names', '')),
                    field_name=str(row.get('Field_Name', '')),
                    match_criteria=str(row.get('Match_Criteria', '')),
                    status=str(row.get('Status', ''))
                ))
        return active_rules

    @staticmethod
    def clean(val) -> str:
        if val is None or str(val).lower() in ['nan', 'null', 'none']:
            return ""
        return str(val).strip().lower()

    def evaluate(self, jarvis_data: dict, extracted_fields: dict) -> dict:
        report = {
            "application_status": "Approved",
            "total_rules_evaluated": 0,
            "failures": 0,
            "details": []
        }
        for rule in self.rules:
            field_name = rule.field_name
            val_source1 = jarvis_data.get(field_name, "")
            mapped_key = FIELD_MAPPING.get(field_name, field_name.lower().replace(" ", "_"))
            val_source2 = extracted_fields.get(mapped_key, "")
            criteria = rule.match_criteria.strip().lower()
            passed = False
            remarks = ""
            if "presence" in criteria or "unique" in criteria:
                passed = bool(self.clean(val_source1))
                remarks = "Present in App Form" if passed else "Missing in App Form"
                if "presence" in criteria:
                    report["total_rules_evaluated"] += 1
            elif "exact" in criteria:
                report["total_rules_evaluated"] += 1
                if self.clean(val_source1) and self.clean(val_source2):
                    passed = self.clean(val_source1) == self.clean(val_source2)
                    remarks = "Exact Match" if passed else f"Mismatch: '{val_source1}' vs '{val_source2}'"
                else:
                    remarks = "Data missing in one or both sources"
            elif "score" in criteria or "fuzzy" in criteria:
                report["total_rules_evaluated"] += 1
                c1, c2 = self.clean(val_source1), self.clean(val_source2)
                if c1 and c2:
                    score = fuzz.token_sort_ratio(c1, c2)
                    passed = score >= 80
                    remarks = f"Fuzzy Match Score: {score}/100" if passed else f"Low Match Score ({score}): '{val_source1}' vs '{val_source2}'"
                else:
                    remarks = "Data missing in one or both sources"
            if not passed and "presence" not in criteria:
                if self.clean(val_source1) or self.clean(val_source2):
                    report["failures"] += 1
                    report["application_status"] = "Manual Review Required"
            report["details"].append({
                "rule_number": rule.rule_number,
                "entity": rule.entity_name,
                "field": field_name,
                "criteria": rule.match_criteria,
                "passed": passed,
                "remarks": remarks
            })
        return report
