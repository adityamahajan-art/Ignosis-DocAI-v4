import os
import re
import datetime
import pdfplumber
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field, field_validator

# =====================================================================
# PYDANTIC RE-VALIDATION AND STRUCTURAL FINANCIAL SCHEMAS
# =====================================================================

class DocumentMetadata(BaseModel):
    entity_name: str = "Unknown"
    financial_year: str = "Unknown"
    reporting_currency: str = "INR"
    reporting_unit: str = "Absolute"
    is_consolidated: bool = False

class BalanceSheetSchema(BaseModel):
    non_current_assets: Optional[float] = None
    current_assets: Optional[float] = None
    total_assets: Optional[float] = None
    share_capital: Optional[float] = None
    reserves_and_surplus: Optional[float] = None
    total_equity: Optional[float] = None
    non_current_liabilities: Optional[float] = None
    current_liabilities: Optional[float] = None
    total_equity_and_liabilities: Optional[float] = None

class ProfitAndLossSchema(BaseModel):
    revenue_from_operations: Optional[float] = None
    other_income: Optional[float] = None
    total_revenue: Optional[float] = None
    employee_benefit_expenses: Optional[float] = None
    finance_costs: Optional[float] = None
    depreciation_and_amortization: Optional[float] = None
    other_expenses: Optional[float] = None
    total_expenses: Optional[float] = None
    profit_before_tax: Optional[float] = None
    tax_expense: Optional[float] = None
    profit_after_tax: Optional[float] = None
    basic_eps: Optional[float] = None
    diluted_eps: Optional[float] = None

class CashFlowSchema(BaseModel):
    profit_before_tax_reconciliation: Optional[float] = None
    net_cash_from_operating_activities: Optional[float] = None
    net_cash_used_in_investing_activities: Optional[float] = None
    net_cash_used_in_financing_activities: Optional[float] = None
    net_increase_decrease_in_cash: Optional[float] = None
    cash_and_equivalents_opening: Optional[float] = None
    cash_and_equivalents_closing: Optional[float] = None

class PipelineAuditSchema(BaseModel):
    extraction_source_pages: Dict[str, List[int]]
    processing_timestamp: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat() + "Z")
    validation_warnings: List[str] = []

class ComprehensiveFinancialStatement(BaseModel):
    DOCUMENT_METADATA: DocumentMetadata
    BALANCE_SHEET: BalanceSheetSchema
    PROFIT_AND_LOSS: ProfitAndLossSchema
    CASH_FLOW: CashFlowSchema
    PIPELINE_AUDIT: PipelineAuditSchema


# =====================================================================
# UTILITIES AND FINANCIAL DATA CLEANING HELPER
# =====================================================================

def clean_financial_value(raw_val: Any) -> Optional[float]:
    """
    Standardizes accounting formats like '(1,250.50)', '1250.50-', or '—' into a float.
    """
    if raw_val is None:
        return 0.0
    
    s = str(raw_val).strip()
    
    # Check for empty accounting symbols or placeholder tokens
    if s in ["", "—", "–", "-", "._", "nil", "NIL"]:
        return 0.0
        
    try:
        # Determine polarity
        is_negative = False
        if (s.startswith("(") and s.endswith(")")) or s.endswith("-"):
            is_negative = True
            
        # Strip all formatting characters except numbers and decimal delimiters
        s = re.sub(r"[^\d.]", "", s)
        
        if not s:
            return 0.0
            
        val = float(s)
        return -val if is_negative else val
    except ValueError:
        return None


def search_text_for_regex(text: str, regex_patterns: List[str]) -> Optional[float]:
    """
    Fallback regex scanner to seek numerical values trailing specific localized row labels.
    """
    for pattern in regex_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            # Assumes the targeted component grouping encapsulates the digit payload
            try:
                return clean_financial_value(match.group(1))
            except IndexError:
                continue
    return None


# =====================================================================
# NATIVE CORE PROCESSING PIPELINE ENGINE
# =====================================================================

class FinancialStatementEngine:
    def __init__(self, pdf_path: str, coordinates_json: Dict[str, Any]):
        self.pdf_path = pdf_path
        self.coords = coordinates_json
        self.warnings = []
        self.extracted_pages_map = {"balance_sheet": [], "pnl": [], "cash_flow": []}

    def parse_coordinate_map(self) -> Dict[str, List[int]]:
        """Maps out the human page coordinates tracking index positions dynamically."""
        pages_by_type = {"BALANCE_SHEET": [], "PROFIT_AND_LOSS": [], "CASH_FLOW": []}
        
        # Ingest flat coordinates matrix arrays
        for record in self.coords.get("financial_statement_pages", []):
            ptype = record.get("type")
            phuman = record.get("page_number_human")
            if ptype in pages_by_type and phuman is not None:
                pages_by_type[ptype].append(int(phuman))
                
        # Handle explicit structural single blocks
        for key, value in self.coords.items():
            if isinstance(value, dict) and "page_number_human" in value:
                phuman = int(value["page_number_human"])
                if "loss" in key or "pnl" in key:
                    pages_by_type["PROFIT_AND_LOSS"].append(phuman)
                    
        # Deduplicate and register targets
        self.extracted_pages_map["balance_sheet"] = sorted(list(set(pages_by_type["BALANCE_SHEET"])))
        self.extracted_pages_map["pnl"] = sorted(list(set(pages_by_type["PROFIT_AND_LOSS"])))
        self.extracted_pages_map["cash_flow"] = sorted(list(set(pages_by_type["CASH_FLOW"])))

    def extract_page_content(self, page_numbers: List[int]) -> str:
        """Siphons raw text blocks out of target pages via pdfplumber layout engine."""
        combined_text = ""
        if not page_numbers:
            return combined_text
            
        with pdfplumber.open(self.pdf_path) as pdf:
            for p_num in page_numbers:
                # Array index conversion from human numbering formats
                idx = p_num - 1
                if 0 <= idx < len(pdf.pages):
                    page = pdf.pages[idx]
                    text = page.extract_text()
                    if text:
                        combined_text += f"\n--- PAGE {p_num} ---\n" + text
        return combined_text

    def process(self) -> Dict[str, Any]:
        self.parse_coordinate_map()
        
        # Extract individual section blobs
        bs_text = self.extract_page_content(self.extracted_pages_map["balance_sheet"])
        pnl_text = self.extract_page_content(self.extracted_pages_map["pnl"])
        cf_text = self.extract_page_content(self.extracted_pages_map["cash_flow"])
        
        # Global Metadata Analysis
        combined_all = bs_text + pnl_text + pnl_text
        metadata = self._extract_metadata(combined_all)
        
        # Parse Components
        bs_data = self._parse_balance_sheet(bs_text)
        pnl_data = self._parse_pnl(pnl_text)
        cf_data = self._parse_cash_flow(cf_text)
        
        # Execute Cross-Mathematical Equation Checks
        self._perform_mathematical_validation(bs_data, pnl_data)
        
        # Build audited unified container
        audit = PipelineAuditSchema(
            extraction_source_pages=self.extracted_pages_map,
            validation_warnings=self.warnings
        )
        
        unified_output = ComprehensiveFinancialStatement(
            DOCUMENT_METADATA=metadata,
            BALANCE_SHEET=bs_data,
            PROFIT_AND_LOSS=pnl_data,
            CASH_FLOW=cf_data,
            PIPELINE_AUDIT=audit
        )
        
        return unified_output.model_dump()

    # =====================================================================
    # COMPONENT PARSERS AND MATRICES STRIPPERS
    # =====================================================================

    def _extract_metadata(self, text: str) -> DocumentMetadata:
        meta = DocumentMetadata()
        
        # Search Entity Identity Framework
        entity_match = re.search(r"Limited|Ltd|Corporation|Corp|Inc\.", text)
        if entity_match:
            # Look up matching row anchor segment bounds
            lines = text.split("\n")
            for line in lines[:15]:
                if any(x in line for x in ["Limited", "Ltd", "Inc", "CO"]):
                    meta.entity_name = line.strip()
                    break
                    
        # Trace Fiscal Boundaries
        fy_match = re.search(r"(?:Financial\s+Year|FY|Balance\s+Sheet\s+as\s+at\s+31st\s+March,?\s+)(\d{4}[-–]\d{2,4})", text, re.I)
        if fy_match:
            meta.financial_year = fy_match.group(1)
            
        # Currency Identifiers Detection
        if "Rs." in text or "INR" in text or "₹" in text:
            meta.reporting_currency = "INR"
        elif "USD" in text or "$" in text:
            meta.reporting_currency = "USD"
            
        # Identify Scale Unit Definitions
        if "crore" in text.lower():
            meta.reporting_unit = "Crores"
        elif "lakh" in text.lower():
            meta.reporting_unit = "Lakhs"
        elif "million" in text.lower():
            meta.reporting_unit = "Millions"
            
        # Consolidation Status Validation Flag
        if "consolidated" in text.lower():
            meta.is_consolidated = True
            
        return meta

    def _parse_balance_sheet(self, text: str) -> BalanceSheetSchema:
        return BalanceSheetSchema(
            non_current_assets=search_text_for_regex(text, [r"Non-current\s+assets.*?([\d,().—-]+)"]),
            current_assets=search_text_for_regex(text, [r"Current\s+assets.*?([\d,().—-]+)"]),
            total_assets=search_text_for_regex(text, [r"Total\s+assets.*?([\d,().—-]+)"]),
            share_capital=search_text_for_regex(text, [r"Share\s+capital.*?([\d,().—-]+)"]),
            reserves_and_surplus=search_text_for_regex(text, [r"Reserves\s+and\s+surplus.*?([\d,().—-]+)"]),
            total_equity=search_text_for_regex(text, [r"Total\s+equity.*?([\d,().—-]+)"]),
            non_current_liabilities=search_text_for_regex(text, [r"Non-current\s+liabilities.*?([\d,().—-]+)"]),
            current_liabilities=search_text_for_regex(text, [r"Current\s+liabilities.*?([\d,().—-]+)"]),
            total_equity_and_liabilities=search_text_for_regex(text, [r"Total\s+equity\s+and\s+liabilities.*?([\d,().—-]+)", r"Total\s+liabilities.*?([\d,().—-]+)"])
        )

    def _parse_pnl(self, text: str) -> ProfitAndLossSchema:
        return ProfitAndLossSchema(
            revenue_from_operations=search_text_for_regex(text, [r"Revenue\s+from\s+operations.*?([\d,().—-]+)"]),
            other_income=search_text_for_regex(text, [r"Other\s+income.*?([\d,().—-]+)"]),
            total_revenue=search_text_for_regex(text, [r"Total\s+revenue.*?([\d,().—-]+)", r"Total\s+Income.*?([\d,().—-]+)"]),
            employee_benefit_expenses=search_text_for_regex(text, [r"Employee\s+benefit.*?([\d,().—-]+)"]),
            finance_costs=search_text_for_regex(text, [r"Finance\s+costs.*?([\d,().—-]+)"]),
            depreciation_and_amortization=search_text_for_regex(text, [r"Depreciation.*?([\d,().—-]+)"]),
            other_expenses=search_text_for_regex(text, [r"Other\s+expenses.*?([\d,().—-]+)"]),
            total_expenses=search_text_for_regex(text, [r"Total\s+expenses.*?([\d,().—-]+)"]),
            profit_before_tax=search_text_for_regex(text, [r"Profit\s+before\s+tax.*?([\d,().—-]+)"]),
            tax_expense=search_text_for_regex(text, [r"Tax\s+expense.*?([\d,().—-]+)"]),
            profit_after_tax=search_text_for_regex(text, [r"Profit\s+after\s+tax.*?([\d,().—-]+)", r"Profit\s+for\s+the\s+period.*?([\d,().—-]+)"]),
            basic_eps=search_text_for_regex(text, [r"Basic\s+EPS.*?([\d,().—-]+)", r"Basic\s+\(in\s+Rs\.\).*?([\d,().—-]+)"]),
            diluted_eps=search_text_for_regex(text, [r"Diluted\s+EPS.*?([\d,().—-]+)", r"Diluted\s+\(in\s+Rs\.\).*?([\d,().—-]+)"])
        )

    def _parse_cash_flow(self, text: str) -> CashFlowSchema:
        return CashFlowSchema(
            profit_before_tax_reconciliation=search_text_for_regex(text, [r"Profit\s+before\s+tax.*?([\d,().—-]+)"]),
            net_cash_from_operating_activities=search_text_for_regex(text, [r"Net\s+cash\s+(?:generated\s+from|from)\s+operating\s+activities.*?([\d,().—-]+)"]),
            net_cash_used_in_investing_activities=search_text_for_regex(text, [r"Net\s+cash\s+(?:used\s+in|from)\s+investing\s+activities.*?([\d,().—-]+)"]),
            net_cash_used_in_financing_activities=search_text_for_regex(text, [r"Net\s+cash\s+(?:used\s+in|from)\s+financing\s+activities.*?([\d,().—-]+)"]),
            net_increase_decrease_in_cash=search_text_for_regex(text, [r"Net\s+(?:increase|decrease)\s+in\s+cash.*?([\d,().—-]+)"]),
            cash_and_equivalents_opening=search_text_for_regex(text, [r"Cash\s+and\s+cash\s+equivalents\s+at\s+the\s+beginning.*?([\d,().—-]+)"]),
            cash_and_equivalents_closing=search_text_for_regex(text, [r"Cash\s+and\s+cash\s+equivalents\s+at\s+the\s+end.*?([\d,().—-]+)"])
        )

    # =====================================================================
    # MATHEMATICAL RECONCILIATION LAYER
    # =====================================================================

    def _perform_mathematical_validation(self, bs: BalanceSheetSchema, pnl: ProfitAndLossSchema):
        tolerance = 1.0  # Max currency rounding variance limit
        
        # 1. Asset Equation Check: Total Assets = Total Equity + Liabilities
        ta = bs.total_assets or 0.0
        tel = bs.total_equity_and_liabilities or 0.0
        if abs(ta - tel) > tolerance:
            self.warnings.append(f"Balance Sheet Imbalance: Total Assets ({ta}) != Total Equity & Liabilities ({tel})")
            
        # 2. Income Equation Check: Total Revenue - Total Expenses = PBT
        tr = pnl.total_revenue or 0.0
        te = pnl.total_expenses or 0.0
        pbt = pnl.profit_before_tax or 0.0
        if abs((tr - te) - pbt) > tolerance:
            self.warnings.append(f"P&L Accounting Warning: Calculated Revenue minus Expenses ({(tr - te):.2f}) does not match explicit PBT ({pbt})")