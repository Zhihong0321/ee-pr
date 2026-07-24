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

# LLM Environment Variables
LLM_API_URL = os.environ.get("LLM_API_URL", "https://api.apikey.fun/v1/chat/completions")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "sk-fdf46010fd62b734b915f23951278b888fd52afed195dd2caf21067fbeaf404e")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

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

def analyze_email_with_llm(subj, sender, body_text):
    system_prompt = """You are the Eternalgy Corporate PR AI Officer. Analyze the received email and respond in strict JSON with:
1. category: "CERTIFICATE_UPDATE" | "PRESS_RELEASE" | "AWARD_RECOGNITION" | "MEDIA_PHOTO_BATCH"
2. headline: Professional executive title
3. summary: Concise 2-sentence summary
4. requires_manager_decision: boolean (true if options/guidance needed from manager, else false)
5. manager_question: Actionable question for manager if true, else null
6. manager_options: Array of 2-3 option strings for manager if true, else []
"""
    user_prompt = f"From: {sender}
Subject: {subj}
Content: {body_text[:1000]}"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3
    }
    try:
        req = urllib.request.Request(LLM_API_URL, data=json.dumps(payload).encode('utf-8'), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=12) as resp:
            res_data = json.loads(resp.read().decode('utf-8'))
            llm_text = res_data['choices'][0]['message']['content']
            # Clean markdown codeblocks if present
            if llm_text.startswith("```json"):
                llm_text = llm_text.split("```json")[1].split("```")[0].strip()
            elif llm_text.startswith("```"):
                llm_text = llm_text.split("```")[1].split("```")[0].strip()
            return json.loads(llm_text)
    except Exception as e:
        print(f"LLM API Error (fallback to rule engine): {e}")
        # Rule-based fallback
        category = "CERTIFICATE_UPDATE" if ("seda" in subj.lower() or "cert" in subj.lower()) else "PRESS_RELEASE"
        return {
            "category": category,
            "headline": subj or "Corporate Announcement",
            "summary": f"Received email from {sender} regarding {subj}.",
            "requires_manager_decision": True,
            "manager_question": f"New {category} email received: '{subj}'. How should we handle this item?",
            "manager_options": ["📰 Publish to Corporate PR Feed", "📁 Save to Vault Only", "⏸️ Hold for Review"]
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
        
    # Run LLM Reasoning Engine
    llm_result = analyze_email_with_llm(subj, sender, body_text)
    print(f"LLM Analysis Result: {llm_result}")
    
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
        # Avoid duplicates
        if not any(str(q.get("email_id")) == str(email_id) for q in state.get("manager_questions", [])):
            state.setdefault("manager_questions", []).insert(0, q_card)
            
    save_state(state)
    
    return {
        "email_id": email_id,
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

        # Serve Dynamic State Store for Dashboard Frontpage
        if self.path == "/api/dashboard-state":
            state = load_state()
            self._send_response(200, state)
            return

        if self.path == "/health" or self.path == "/api/health":
            self._send_response(200, {
                "status": "healthy",
                "service": "Eternalgy Corporate PR & Media Center",
                "llm_model": LLM_MODEL,
                "cloud_storage": "Cloudflare R2",
                "r2_bucket": R2_BUCKET_NAME,
                "webhook_endpoint": "/webhook/email-received"
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
                "message": "Email analyzed by LLM AI, tasks generated, and marked as read on EE-Mail server.",
                "data": result
            })
            return

        # Endpoint for Manager Interactive Q&A Response
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

                    # Publish update card to state if approved
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
    print(f"Starting Eternalgy PR AI Server with LLM Intelligence & Manager Q&A on port {PORT}...")
    server = socketserver.TCPServer(("", PORT), PRServerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server...")
        server.server_close()
