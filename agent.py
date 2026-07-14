import os
import re
import sys
import uuid
import json
import logging
from datetime import datetime
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import APIError

# ==========================================
# 1. Python Structured Logging with JSON Formatter
# ==========================================
_trace_context = {"trace_id": None, "span_id": None}

def get_trace_context():
    if not _trace_context["trace_id"]:
        _trace_context["trace_id"] = str(uuid.uuid4())
        _trace_context["span_id"] = str(uuid.uuid4())[:16]
    return _trace_context

class JSONLogFormatter(logging.Formatter):
    def format(self, record):
        ctx = get_trace_context()
        log_record = {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "severity": record.levelname,
            "component": record.name,
            "trace_id": ctx["trace_id"],
            "span_id": ctx["span_id"],
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_info"):
            log_record.update(record.extra_info)
        return json.dumps(log_record)

# Configure logger
logger = logging.getLogger("ambient_expense_agent")
logger.setLevel(logging.INFO)
# Clear existing handlers
for handler in logger.handlers[:]:
    logger.removeHandler(handler)
    
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(JSONLogFormatter())
logger.addHandler(stdout_handler)

# ==========================================
# 2. OpenTelemetry Tracing Setup
# ==========================================
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter

# Initialize Tracer Provider with Console Span Exporter
provider = TracerProvider()
trace.set_tracer_provider(provider)
# Output traces to standard console stream
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stdout)))
tracer = trace.get_tracer("ambient_expense_agent")

def set_trace_context(trace_id: str, span_id: str = None):
    _trace_context["trace_id"] = trace_id
    _trace_context["span_id"] = span_id or str(uuid.uuid4())[:16]


# PII Scrubbing Rules
PII_RULES = {
    "email": (re.compile(r"[\w\.-]+@[\w\.-]+\.\w+"), "[REDACTED_EMAIL]"),
    "phone": (re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"), "[REDACTED_PHONE]"),
    "ssn": (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    "credit_card": (re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"), "[REDACTED_CARD]")
}

def scan_and_redact_pii(text: str) -> tuple[str, list[str]]:
    """Scans description text for PII and redacts matches, returning the redacted text and categories found."""
    redacted_text = text
    categories_found = []
    for name, (pattern, replacement) in PII_RULES.items():
        if pattern.search(redacted_text):
            redacted_text = pattern.sub(replacement, redacted_text)
            categories_found.append(name)
    return redacted_text, categories_found

# Prompt Injection Safety Rules
PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"system\s+prompt\s+override", re.IGNORECASE),
    re.compile(r"instead\s+of\s+approving\s+or\s+rejecting", re.IGNORECASE),
    re.compile(r"you\s+must\s+approve\s+this", re.IGNORECASE),
    re.compile(r"bypass\s+the\s+policy", re.IGNORECASE)
]

def detect_prompt_injection(text: str) -> bool:
    """Detects potential adversarial prompt injection attempts in input text."""
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


# ==========================================
# 3. Define Expense State Pydantic Models
# ==========================================
class ExpenseState(BaseModel):
    expense_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8], description="Unique ID of the claim")
    amount: float = Field(..., description="Expense amount in USD")
    description: str = Field(..., description="Details and context about the expense")
    status: str = Field("pending", description="Current status of the claim (pending, approved, rejected)")
    approver_notes: str = Field("", description="Reason/Notes from the auditor")


# ==========================================
# 4. Strict JSON Schemas for Tool Validation
# ==========================================
tool_approve = types.FunctionDeclaration(
    name="approve_expense_claim",
    description="Automatically approves a standard expense claim that is under $100.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "expense_id": types.Schema(type=types.Type.STRING, description="Unique identifier for the expense claim."),
            "amount": types.Schema(type=types.Type.NUMBER, description="The dollar amount of the expense. Must be strictly less than 100.00."),
            "description": types.Schema(type=types.Type.STRING, description="The description of the item or service purchased."),
            "reason": types.Schema(type=types.Type.STRING, description="Detailed justification explaining why this qualifies for automatic approval.")
        },
        required=["expense_id", "amount", "description", "reason"]
    )
)

tool_flag = types.FunctionDeclaration(
    name="flag_expense_for_human_review",
    description="Flags an expense claim for human auditor review. Required for all claims of $100 or more, or suspicious descriptions.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "expense_id": types.Schema(type=types.Type.STRING, description="Unique identifier for the expense claim."),
            "amount": types.Schema(type=types.Type.NUMBER, description="The dollar amount of the expense."),
            "description": types.Schema(type=types.Type.STRING, description="The description of the item or service purchased."),
            "reason": types.Schema(type=types.Type.STRING, description="Detailed justification of why this claim requires human review.")
        },
        required=["expense_id", "amount", "description", "reason"]
    )
)

tool_reject = types.FunctionDeclaration(
    name="reject_expense_claim",
    description="Rejects an expense claim that violates corporate spending policy.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "expense_id": types.Schema(type=types.Type.STRING, description="Unique identifier for the expense claim."),
            "amount": types.Schema(type=types.Type.NUMBER, description="The dollar amount of the expense."),
            "description": types.Schema(type=types.Type.STRING, description="The description of the item or service purchased."),
            "reason": types.Schema(type=types.Type.STRING, description="Detailed justification explaining the policy violation.")
        },
        required=["expense_id", "amount", "description", "reason"]
    )
)

# Tool Execution Methods
def execute_approve_expense_claim(expense_id: str, amount: float, description: str, reason: str) -> dict:
    if amount >= 100.0:
        raise ValueError(f"Automatic approval is restricted to claims under $100. Attempted to auto-approve ${amount:.2f}.")
    return {
        "status": "approved",
        "approver_notes": f"Automatically approved: ${amount:.2f} for '{description}'. Reason: {reason}"
    }

def execute_flag_expense_for_human_review(expense_id: str, amount: float, description: str, reason: str) -> dict:
    return {
        "status": "pending",
        "approver_notes": f"Flagged for manual review: ${amount:.2f} for '{description}'. Reason: {reason}"
    }

def execute_reject_expense_claim(expense_id: str, amount: float, description: str, reason: str) -> dict:
    return {
        "status": "rejected",
        "approver_notes": f"Rejected: ${amount:.2f} for '{description}'. Reason: {reason}"
    }


# ==========================================
# 5. Category Specialist Agents (Multi-Agent Patterns)
# ==========================================
class TravelAuditorAgent:
    def __init__(self, client: genai.Client):
        self.client = client
        self.system_instruction = (
            "You are a Specialist Travel Auditor Agent. You specialize in auditing travel, accommodation, "
            "taxi fares, hotel costs, flights, and meals during business trips. You MUST call exactly ONE of the tools "
            "to declare your decision: approve_expense_claim (if < $100 and valid), flag_expense_for_human_review (if >= $100 or suspicious), "
            "or reject_expense_claim (if violating travel policies e.g. booking first class flights or luxury retreats)."
        )

class ITEquipmentAuditorAgent:
    def __init__(self, client: genai.Client):
        self.client = client
        self.system_instruction = (
            "You are a Specialist IT & Software Auditor Agent. You specialize in auditing office computer screens, "
            "keyboards, software licenses, mouse accessories, and tech subscriptions. You MUST call exactly ONE of the tools "
            "to declare your decision: approve_expense_claim (if < $100 and valid), flag_expense_for_human_review (if >= $100 or suspicious), "
            "or reject_expense_claim (if personal e.g. buying gaming consoles or video games)."
        )

class GeneralAuditorAgent:
    def __init__(self, client: genai.Client):
        self.client = client
        self.system_instruction = (
            "You are a General Expense Auditor Agent. You audit office stationery, groceries, team building, "
            "and other general utility categories. You MUST call exactly ONE of the tools "
            "to declare your decision: approve_expense_claim (if < $100 and valid), flag_expense_for_human_review (if >= $100 or suspicious), "
            "or reject_expense_claim (if clearly personal, non-business related, or excessive)."
        )


# ==========================================
# 6. Core Multi-Agent Orchestrator
# ==========================================
class ExpenseAuditorAgent:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            logger.warning("Missing GEMINI_API_KEY environment variable. Fallback behavior will be used.")
            
        self.client = genai.Client(api_key=self.api_key) if self.api_key else None
        
        # Instantiate sub-agents
        if self.client:
            self.travel_specialist = TravelAuditorAgent(self.client)
            self.it_specialist = ITEquipmentAuditorAgent(self.client)
            self.general_specialist = GeneralAuditorAgent(self.client)

    def process_claim(self, claim: ExpenseState) -> ExpenseState:
        """Processes an incoming expense claim with OpenTelemetry spans, PII filters, and Multi-Agent Routing."""
        
        # Start OpenTelemetry Tracing Span
        with tracer.start_as_current_span("process_claim") as span:
            span.set_attribute("expense.id", claim.expense_id)
            span.set_attribute("expense.amount", claim.amount)
            
            logger.info(f"Auditor received claim {claim.expense_id}", extra={"extra_info": {
                "amount": claim.amount,
                "description": claim.description
            }})

            # 1. PII Redaction
            clean_desc, redacted_categories = scan_and_redact_pii(claim.description)
            if redacted_categories:
                logger.info("PII Scrubbing executed successfully", extra={"extra_info": {
                    "redacted_categories": redacted_categories
                }})
                claim.description = clean_desc

            # 2. Prompt Injection Guardrail
            if detect_prompt_injection(claim.description):
                logger.warning(f"Adversarial prompt injection detected in claim {claim.expense_id}", extra={"extra_info": {
                    "raw_input": claim.description
                }})
                claim.status = "rejected"
                claim.approver_notes = "Rejected: Prompt injection security violation detected."
                span.set_attribute("security.violation", True)
                return claim

            # If no API key is available, fallback to a robust rule-based model to allow basic pass
            if not self.client:
                logger.warning("Running local rule-based fallback due to missing Gemini API Key.")
                if claim.amount >= 100.0:
                    result = execute_flag_expense_for_human_review(claim.expense_id, claim.amount, claim.description, "Amount meets or exceeds $100 threshold (Mock Fallback).")
                elif "personal" in claim.description.lower() or "luxury" in claim.description.lower() or "video game" in claim.description.lower() or "switch" in claim.description.lower():
                    result = execute_reject_expense_claim(claim.expense_id, claim.amount, claim.description, "Mock Fallback: Personal items are not reimbursable.")
                else:
                    result = execute_approve_expense_claim(claim.expense_id, claim.amount, claim.description, "Mock Fallback: Standard business expense under $100.")
                claim.status = result["status"]
                claim.approver_notes = result["approver_notes"]
                return claim

            # 3. Router Agent & Strategic Model Routing
            # Step 3a: Classify the category using a fast model (gemini-2.0-flash)
            router_prompt = (
                f"Classify the following expense description into one of: 'travel', 'it_equipment', 'general'.\n"
                f"Description: {claim.description}\n"
                f"Output only the category name."
            )
            
            try:
                router_response = self.client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=router_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0
                    )
                )
                category = router_response.text.strip().lower()
                span.set_attribute("agent.routing_category", category)
                logger.info(f"Router Agent categorized claim {claim.expense_id} as: {category}")
                
                # Step 3b: Strategic Model Routing based on complexity
                # If claim is large (>= $100), route to a larger reasoning model (gemini-2.5-pro)
                # Else route to a fast model (gemini-2.0-flash)
                if claim.amount >= 100.0:
                    audit_model = "gemini-2.5-pro"
                else:
                    audit_model = "gemini-2.0-flash"
                    
                span.set_attribute("agent.audit_model", audit_model)
                logger.info(f"Strategic routing selected audit model: {audit_model}")
                
                # Fetch sub-agent instructions
                if "travel" in category:
                    sub_agent = self.travel_specialist
                elif "it" in category or "monitor" in category or "keyboard" in category or "software" in category:
                    sub_agent = self.it_specialist
                else:
                    sub_agent = self.general_specialist
                    
                # 4. Self-Recovery Loop
                # Run the specialist agent audit with error instructions returned to LLM for correction
                max_attempts = 3
                attempt = 0
                history = [types.Content(role="user", parts=[types.Part.from_text(f"Audit this expense claim:\nID: {claim.expense_id}\nAmount: ${claim.amount:.2f}\nDescription: {claim.description}")])]
                
                while attempt < max_attempts:
                    logger.info(f"Auditing claim {claim.expense_id} (Attempt {attempt+1}/{max_attempts}) using {audit_model}")
                    
                    response = self.client.models.generate_content(
                        model=audit_model,
                        contents=history,
                        config=types.GenerateContentConfig(
                            system_instruction=sub_agent.system_instruction,
                            tools=[types.Tool(function_declarations=[tool_approve, tool_flag, tool_reject])],
                            temperature=0.1
                        )
                    )
                    
                    tool_calls = response.function_calls
                    if not tool_calls:
                        # Fallback if the LLM returns text instead of calling a tool
                        logger.warning("Specialist agent did not return a tool call. Requesting review.")
                        result = execute_flag_expense_for_human_review(claim.expense_id, claim.amount, claim.description, "Agent failed to trigger tool call.")
                        claim.status = result["status"]
                        claim.approver_notes = result["approver_notes"]
                        break
                        
                    tool_call = tool_calls[0]
                    args = tool_call.args
                    
                    # Tool call validation & Self-recovery execution
                    error_msg = None
                    try:
                        if tool_call.name == "approve_expense_claim":
                            result = execute_approve_expense_claim(
                                expense_id=args.get("expense_id", claim.expense_id),
                                amount=float(args.get("amount", claim.amount)),
                                description=args.get("description", claim.description),
                                reason=args.get("reason", "")
                            )
                        elif tool_call.name == "flag_expense_for_human_review":
                            result = execute_flag_expense_for_human_review(
                                expense_id=args.get("expense_id", claim.expense_id),
                                amount=float(args.get("amount", claim.amount)),
                                description=args.get("description", claim.description),
                                reason=args.get("reason", "")
                            )
                        elif tool_call.name == "reject_expense_claim":
                            result = execute_reject_expense_claim(
                                expense_id=args.get("expense_id", claim.expense_id),
                                amount=float(args.get("amount", claim.amount)),
                                description=args.get("description", claim.description),
                                reason=args.get("reason", "")
                            )
                        else:
                            raise ValueError(f"Unknown tool name: {tool_call.name}")
                            
                        # Success! Record final decision
                        claim.status = result["status"]
                        claim.approver_notes = result["approver_notes"]
                        logger.info(f"Audit decision successfully verified: {claim.status.upper()}")
                        break
                        
                    except (ValueError, TypeError) as e:
                        # Validation failed! Feed the error back to the LLM for self-recovery
                        error_msg = str(e)
                        attempt += 1
                        logger.warning(f"Auditor validation failure on attempt {attempt}: {error_msg}. Initiating self-recovery loop.")
                        
                        # Add current turn to conversational history
                        history.append(response.candidates[0].content)
                        # Construct error response to feed back
                        func_response_part = types.Part.from_function_response(
                            name=tool_call.name,
                            response={"error": f"Tool validation error: {error_msg}. Please audit the claim and choose the correct tool."}
                        )
                        history.append(types.Content(role="user", parts=[func_response_part]))
                        
                if attempt >= max_attempts:
                    logger.error(f"Agent failed to recover after {max_attempts} attempts. Defaulting to review.")
                    result = execute_flag_expense_for_human_review(claim.expense_id, claim.amount, claim.description, "Agent failed to audit claim successfully after self-recovery retries.")
                    claim.status = result["status"]
                    claim.approver_notes = result["approver_notes"]
                
                return claim
                
            except APIError as e:
                logger.error(f"Gemini API Exception occurred: {str(e)}")
                claim.status = "pending"
                claim.approver_notes = f"API Error: {str(e)}. Flagged for manual review."
                return claim
            except Exception as e:
                logger.error(f"Unexpected processing exception: {str(e)}")
                claim.status = "pending"
                claim.approver_notes = f"System Error: {str(e)}. Flagged for manual review."
                return claim
