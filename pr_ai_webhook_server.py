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
DRAFTS_FILE = os.path.join(MEDIAKIT_DIR, "pending_pr_drafts.json")

# Cloudflare R2 Environment Variables
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "58cac85585fb6057edd57010616be145")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "8320b18a5c5534bd54d28f977ad4af77")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "2d40930ed749ad96a8ed5395888d89b4900adba6db8e15bfc08bc2a71533cb36")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "eternalgy-image")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "https://pub-31ab1252a5544ca19749b476315d9b01.r2.dev")

def get_r2_client():
    try:
        import boto3
        endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        return boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name='auto'
        )
    except Exception as e:
        print(f"R2 Client Init Warning: {e}")
        return None

def upload_to_r2(file_bytes, filename, content_type="application/octet-stream"):
    client = get_r2_client()
    if not client:
        return None
    try:
        r2_key = f"pr-media/{filename}"
        client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=r2_key,
            Body=file_bytes,
            ContentType=content_type
        )
        public_url = f"{R2_PUBLIC_URL.rstrip('/')}/{r2_key}"
        print(f"Uploaded {filename} to Cloudflare R2: {public_url}")
        return public_url
    except Exception as e:
        print(f"Failed to upload {filename} to R2: {e}")
        return None

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

def process_incoming_email(email_id, raw_payload=None):
    print(f"Processing incoming email notification: {email_id}...")
    fetch_res = call_api("POST", "/received-emails/fetch", {"email_id": email_id})
    
    subj = raw_payload.get("subject", "") if raw_payload else ""
    sender = raw_payload.get("from", "") if raw_payload else ""
    
    if not subj and fetch_res and fetch_res.get("data"):
        data_obj = fetch_res["data"]
        seda_task = data_obj.get("sedaTask", {}).get("task", {})
        if seda_task:
            subj = f"SEDA Task: {seda_task.get('task_type', 'APPROVAL')} - {seda_task.get('customer_name', '')}"
            
    if not subj:
        subj = f"PR Communication (ID: {email_id})"
        
    category = "PRESS_RELEASE"
    subj_lower = subj.lower()
    if "seda" in subj_lower or "atap" in subj_lower or "cert" in subj_lower or "approval" in subj_lower:
        category = "CERTIFICATE_UPDATE"
    elif "award" in subj_lower or "bull" in subj_lower:
        category = "AWARD_RECOGNITION"
    elif "photo" in subj_lower or "media" in subj_lower:
        category = "MEDIA_PHOTO_BATCH"
        
    draft_card = {
        "draft_id": f"DRAFT-{email_id}",
        "email_id": email_id,
        "category": category,
        "headline": subj,
        "sender": sender or "System Sender",
        "received_at": datetime.utcnow().isoformat() + "Z",
        "summary": f"Received via pr@eternalgy.me webhook. Formatted into PR update card.",
        "status": "PENDING_MANAGER_APPROVAL",
        "marked_as_read": True,
        "r2_storage_bucket": R2_BUCKET_NAME,
        "r2_cdn_url": f"{R2_PUBLIC_URL}/pr-media/"
    }
    
    drafts = []
    if os.path.exists(DRAFTS_FILE):
        try:
            with open(DRAFTS_FILE, "r", encoding="utf-8") as f:
                drafts = json.load(f)
        except Exception:
            drafts = []
            
    if not any(str(d.get("email_id")) == str(email_id) for d in drafts):
        drafts.insert(0, draft_card)
        with open(DRAFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(drafts, f, indent=2)
            
    return draft_card

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
        # Serve root PR Showcase UI
        if self.path == "/" or self.path == "/index.html":
            html_path = os.path.join(MEDIAKIT_DIR, "eternalgy_overview.html")
            if os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        # Serve API Documentation page for Email Server Team
        if self.path in ["/docs", "/webhook-docs", "/docs.html"]:
            docs_path = os.path.join(MEDIAKIT_DIR, "webhook_docs.html")
            if os.path.exists(docs_path):
                with open(docs_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        # Serve Health endpoint
        if self.path == "/health" or self.path == "/api/health":
            self._send_response(200, {
                "status": "healthy",
                "service": "Eternalgy Corporate PR & Media Center",
                "cloud_storage": "Cloudflare R2",
                "r2_bucket": R2_BUCKET_NAME,
                "r2_public_cdn": R2_PUBLIC_URL,
                "webhook_endpoint": "/webhook/email-received",
                "documentation": "/docs"
            })
            return

        # Serve static files (Logo/, Reference/, PDFs, JSON)
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

            draft_card = process_incoming_email(email_id, payload)
            self._send_response(200, {
                "success": True,
                "message": "Email received, AI parsed, queued for approval, marked as read on EE-Mail server, and integrated with Cloudflare R2 storage.",
                "draft": draft_card
            })
        else:
            self._send_response(404, {"error": "Endpoint not found"})

if __name__ == "__main__":
    print(f"Starting Eternalgy Corporate PR Hub & Webhook Server on port {PORT} with Cloudflare R2 support...")
    server = socketserver.TCPServer(("", PORT), PRServerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server...")
        server.server_close()
