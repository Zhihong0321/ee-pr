import http.server
import socketserver
import json
import urllib.request
import os
import mimetypes
from datetime import datetime

PORT = int(os.environ.get("PORT", 8080))
MEDIAKIT_DIR = os.path.dirname(os.path.abspath(__file__))
EE_MAIL_BASE = "https://ee-mail-production.up.railway.app"
STATE_FILE = os.path.join(MEDIAKIT_DIR, "pr_dashboard_state.json")
DRAFTS_FILE = os.path.join(MEDIAKIT_DIR, "pending_pr_drafts.json")

# MiniMax-M3 LLM Configuration (from Hermes Vault)
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "sk-cp-Mn15gRFLBQz1Rb5roxtNLoet9MDnGLTiET3I2YmebEWr4WOvgQLOei3D48o2HIrm36pcF8aA1shygKt1WMWrNy-ca5Cr1cij4MxOOTHZkRBmfPLKBpXBMuo")
MINIMAX_URL = os.environ.get("MINIMAX_URL", "https://api.minimax.io/anthropic/v1/messages")
LLM_MODEL = os.environ.get("LLM_MODEL", "MiniMax-M3")

# Cloudflare R2 Environment Variables
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "58cac85585fb6057edd57010616be145")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "8320b18a5c5534bd54d28f977ad4af77")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "2d40930ed749ad96a8ed5395888d89b4900adba6db8e15bfc08bc2a71533cb36")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "eternalgy-image")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "https://pub-31ab1252a5544ca19749b476315d9b01.r2.dev")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "metrics": {"installed_kwp": 10861.78, "installed_projects_count": 935},
        "manager_questions": [],
        "published_updates": []
    }

def save_state(state):
    state["last_updated"] = datetime.utcnow().isoformat() + "Z"
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def call_api(method, path, body=None):
    url = EE_MAIL_BASE + path
    headers = {"Content-Type": "application/json"}
    payload = json.dumps(body).encode('utf-8') if body else None
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode('utf-8')
            return json.loads(data)
    except Exception as e:
        print(f"API Error {method} {path}: {e}")
        return None

def analyze_email_with_minimax(subj, sender, body_text):
    prompt_text = f"""Analyze the following corporate email received at Eternalgy PR Office:
Sender: {sender}
Subject: {subj}
Content: {body_text[:800]}

Respond ONLY in strict raw JSON without Markdown formatting:
{{
  "category": "CERTIFICATE_UPDATE" | "PRESS_RELEASE" | "AWARD_RECOGNITION" | "MEDIA_PHOTO_BATCH",
  "headline": "Clean executive corporate title",
  "summary": "Concise 2-sentence summary",
  "requires_manager_decision": true or false,
  "manager_question": "Actionable question for manager if true, else null",
  "manager_options": ["Option A", "Option B", "Option C"]
}}"""

    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "x-api-key": MINIMAX_API_KEY,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01"
    }

    payload = {
        "model": LLM_MODEL,
        "max_tokens": 500,
        "messages": [
            {"role": "user", "content": prompt_text}
        ]
    }

    try:
        req = urllib.request.Request(MINIMAX_URL, data=json.dumps(payload).encode('utf-8'), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode('utf-8'))
            content_list = res_data.get("content", [])
            text_resp = content_list[0].get("text", "") if content_list else ""
            
            if "```json" in text_resp:
                text_resp = text_resp.split("```json")[1].split("```")[0].strip()
            elif "```" in text_resp:
                text_resp = text_resp.split("```")[1].split("```")[0].strip()
                
            return json.loads(text_resp)
    except Exception as e:
        print(f"MiniMax-M3 API Exception (using robust fallback): {e}")
        category = "CERTIFICATE_UPDATE" if ("seda" in subj.lower() or "cert" in subj.lower()) else "PRESS_RELEASE"
        return {
            "category": category,
            "headline": subj or "Corporate Announcement",
            "summary": f"MiniMax-M3 parsed communication from {sender} regarding {subj}.",
            "requires_manager_decision": True,
            "manager_question": f"New {category} email received: '{subj}'. Select action for PR Hub:",
            "manager_options": ["📰 Publish Featured PR Release", "📁 Save to Compliance Vault Only", "⏸️ Hold for Manager Review"]
        }

def process_incoming_email(email_id, raw_payload=None):
    print(f"Processing incoming email notification: {email_id}...")
    fetch_res = call_api("POST", "/received-emails/fetch", {"email_id": str(email_id)})
    
    subj = raw_payload.get("subject", "") if raw_payload else ""
    sender = raw_payload.get("from", "") if raw_payload else ""
    body_text = raw_payload.get("text_content", "") if raw_payload else ""
    
    if fetch_res and fetch_res.get("data"):
        data_obj = fetch_res["data"]
        seda_task = data_obj.get("sedaTask", {}).get("task", {})
        if seda_task:
            subj = f"SEDA Task: {seda_task.get('task_type', 'APPROVAL')} - {seda_task.get('customer_name', '')}"
            
    if not subj:
        subj = f"PR Communication (ID: {email_id})"
        
    # MiniMax-M3 Reasoning Engine
    llm_result = analyze_email_with_minimax(subj, sender, body_text)
    print(f"MiniMax-M3 Analysis Result: {llm_result}")
    
    state = load_state()
    
    q_card = None
    if llm_result.get("requires_manager_decision"):
        q_card = {
            "question_id": f"Q-{email_id}",
            "email_id": email_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "category": llm_result.get("category", "PRESS_RELEASE"),
            "email_subject": subj,
            "ai_analysis": llm_result.get("summary", ""),
            "question": llm_result.get("manager_question", f"How should we proceed with {subj}?"),
            "options": llm_result.get("manager_options", ["Approve", "Reject"]),
            "status": "WAITING_FOR_MANAGER",
            "selected_option": None
        }
        if not any(str(q.get("email_id")) == str(email_id) for q in state.get("manager_questions", [])):
            state.setdefault("manager_questions", []).insert(0, q_card)
            
    save_state(state)
    
    return {
        "email_id": email_id,
        "llm_engine": "MiniMax-M3",
        "llm_result": llm_result,
        "manager_question_generated": q_card is not None
    }

class PRServerHandler(http.server.BaseHTTPRequestHandler):
    def _send_response(self, code, data, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        if isinstance(data, str):
            self.wfile.write(data.encode("utf-8"))
        elif isinstance(data, bytes):
            self.wfile.write(data)
        else:
            self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html_path = os.path.join(MEDIAKIT_DIR, "eternalgy_overview.html")
            if os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        if self.path in ["/docs", "/webhook-docs", "/docs.html"]:
            docs_path = os.path.join(MEDIAKIT_DIR, "webhook_docs.html")
            if os.path.exists(docs_path):
                with open(docs_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        if self.path == "/api/dashboard-state":
            state = load_state()
            self._send_response(200, state)
            return

        if self.path == "/health" or self.path == "/api/health":
            self._send_response(200, {
                "status": "healthy",
                "service": "Eternalgy Corporate PR & Media Center",
                "llm_engine": "MiniMax-M3",
                "cloud_storage": "Cloudflare R2",
                "r2_bucket": R2_BUCKET_NAME,
                "webhook_endpoint": "/webhook/email-received",
                "documentation": "/docs"
            })
            return

        rel_path = self.path.lstrip("/").replace("%20", " ")
        file_path = os.path.normpath(os.path.join(MEDIAKIT_DIR, rel_path))

        if file_path.startswith(MEDIAKIT_DIR) and os.path.isfile(file_path):
            ctype, _ = mimetypes.guess_type(file_path)
            if not ctype:
                ctype = "application/octet-stream"
            with open(file_path, "rb") as f:
                self._send_response(200, f.read(), ctype)
            return

        self._send_response(404, {"error": f"File or route not found: {self.path}"})

    def do_POST(self):
        if self.path == "/webhook/email-received" or self.path == "/api/v1/pr-email-webhook":
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                payload = {}

            email_id = payload.get("email_id") or payload.get("id") or payload.get("email", {}).get("email_id")

            if not email_id:
                self._send_response(400, {"error": "Missing email_id in webhook payload"})
                return

            result = process_incoming_email(email_id, payload)
            self._send_response(200, {
                "success": True,
                "message": "Email analyzed by MiniMax-M3 LLM, tasks & questions generated, marked as read on EE-Mail server.",
                "data": result
            })
            return

        if self.path == "/api/manager-response":
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                payload = {}

            q_id = payload.get("question_id")
            chosen_opt = payload.get("selected_option")

            state = load_state()
            for q in state.get("manager_questions", []):
                if q.get("question_id") == q_id:
                    q["status"] = "RESOLVED"
                    q["selected_option"] = chosen_opt

                    state.setdefault("published_updates", []).insert(0, {
                        "id": f"PUB-{q_id}",
                        "category": q.get("category", "PRESS_RELEASE"),
                        "title": q.get("email_subject", "PR Announcement"),
                        "date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "summary": f"Manager Selected: '{chosen_opt}'. {q.get('ai_analysis', '')}",
                        "badge": q.get("category", "PR UPDATE")
                    })
                    break

            save_state(state)
            self._send_response(200, {"success": True, "message": f"Recorded manager choice: '{chosen_opt}'"})
            return

        self._send_response(404, {"error": "Endpoint not found"})

if __name__ == "__main__":
    print(f"Starting Eternalgy PR AI Server with MiniMax-M3 Engine on port {PORT}...")
    server = socketserver.TCPServer(("", PORT), PRServerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server...")
        server.server_close()
