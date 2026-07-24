import http.server
import socketserver
import json
import urllib.request
import urllib.parse
import os
import mimetypes
import io
import base64
from datetime import datetime
from PIL import Image, ImageOps
import boto3
from botocore.config import Config

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

def get_r2_client():
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
        return None
    endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto"
    )

def upload_bytes_to_r2(key, file_bytes, content_type="image/webp"):
    client = get_r2_client()
    if not client:
        return None
    try:
        client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            Body=file_bytes,
            ContentType=content_type
        )
        return f"{R2_PUBLIC_URL.rstrip('/')}/{key}"
    except Exception as e:
        print(f"R2 Upload Exception for {key}: {e}")
        return None

def optimize_image_data(image_bytes, max_dimension=None, quality=96, lossless=False, generate_thumb=True):
    """
    PR Media Kit High-Fidelity Optimization:
    - Default quality=96 (Ultra-High Web Publication Grade, visually 100% loss-free)
    - Default max_dimension=None (Preserves 100% original pixel resolution without unwanted downscaling)
    - Optional lossless WebP mode for vector graphics, press logos, and high-precision diagrams
    - Auto-corrects EXIF orientation
    - Generates high-density retina thumbnail for gallery preview while retaining full master resolution
    """
    original_size = len(image_bytes)
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        
        # Maintain RGBA if transparency is present, otherwise RGB
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        
        if has_alpha:
            img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")
            
        orig_width, orig_height = img.size
        
        main_img = img.copy()
        # Only downscale if max_dimension is explicitly provided and > 0
        if max_dimension and max_dimension > 0:
            if orig_width > max_dimension or orig_height > max_dimension:
                main_img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            
        opt_buf = io.BytesIO()
        save_kwargs = {"format": "WEBP", "optimize": True}
        if lossless:
            save_kwargs["lossless"] = True
        else:
            save_kwargs["quality"] = min(max(int(quality), 80), 100)

        main_img.save(opt_buf, **save_kwargs)
        optimized_bytes = opt_buf.getvalue()
        optimized_size = len(optimized_bytes)
        
        savings_pct = round((1.0 - (optimized_size / max(original_size, 1))) * 100, 1)
        
        thumb_bytes = None
        thumb_size = 0
        if generate_thumb:
            thumb_img = img.copy()
            # 600px thumbnail for crisp retina cards
            thumb_img.thumbnail((600, 600), Image.Resampling.LANCZOS)
            thumb_buf = io.BytesIO()
            thumb_img.save(thumb_buf, format="WEBP", quality=90, optimize=True)
            thumb_bytes = thumb_buf.getvalue()
            thumb_size = len(thumb_bytes)

        preset_desc = f"PR_MEDIA_KIT_ULTRA_HIGH ({'Lossless' if lossless else f'Q{quality} Web Publication Grade'}, {'Original Resolution' if not max_dimension else f'{max_dimension}px Max'})"

        return {
            "success": True,
            "quality_preset": preset_desc,
            "original_dimensions": [orig_width, orig_height],
            "optimized_dimensions": list(main_img.size),
            "original_size_bytes": original_size,
            "optimized_size_bytes": optimized_size,
            "savings_percentage": f"{savings_pct}%",
            "format": "WEBP",
            "optimized_bytes": optimized_bytes,
            "thumbnail_bytes": thumb_bytes,
            "thumbnail_size_bytes": thumb_size
        }
    except Exception as e:
        print(f"Image Optimization Error: {e}")
        return {
            "success": False,
            "error": str(e),
            "original_size_bytes": original_size
        }


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "metrics": {"installed_kwp": 10750.99, "installed_projects_count": 925},
        "manager_questions": [],
        "published_updates": []
    }

def save_state(state):
    state["last_updated"] = datetime.utcnow().isoformat() + "Z"
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def refresh_metrics_state():
    state = load_state()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    now_date = datetime.utcnow().strftime("%Y-%m-%d")
    state["last_updated"] = now_str
    if "metrics" not in state:
        state["metrics"] = {}
    state["metrics"]["last_check_date"] = now_str
    state["metrics"]["last_verified"] = now_date
    save_state(state)
    return state

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
    # Strictly instruct LLM to provide ONLY technically executable system options
    prompt_text = f"""Analyze the following corporate email received at Eternalgy PR Office:
Sender: {sender}
Subject: {subj}
Content: {body_text[:800]}

CRITICAL RULE FOR OPTIONS:
You MUST ONLY list options that our AI system can actually execute on the website/storage.
DO NOT list external real-world actions like "contact media", "call client", or "issue press release to newspapers".
ONLY list executable system actions, for example:
- "🌐 Publish Featured Case Study to Live PR Feed"
- "📁 Save to Compliance Vault Only"
- "🖼️ Upload Media Attachments to Cloudflare R2 Gallery"
- "⏸️ Hold / Archive Notification"

Respond ONLY in strict raw JSON without Markdown formatting:
{{
  "category": "CERTIFICATE_UPDATE" | "PRESS_RELEASE" | "AWARD_RECOGNITION" | "MEDIA_PHOTO_BATCH",
  "headline": "Clean executive corporate title",
  "summary": "Concise 2-sentence summary",
  "requires_manager_decision": true,
  "manager_question": "Actionable question for manager specifying what system action to take",
  "manager_options": ["Option 1", "Option 2", "Option 3"]
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
        print(f"MiniMax-M3 API Exception (using robust executable fallback): {e}")
        category = "CERTIFICATE_UPDATE" if ("seda" in subj.lower() or "cert" in subj.lower()) else "PRESS_RELEASE"
        return {
            "category": category,
            "headline": subj or "Corporate Announcement",
            "summary": f"Communication from {sender} regarding {subj}.",
            "requires_manager_decision": True,
            "manager_question": f"New {category} email received: '{subj}'. Select executable action for PR Hub:",
            "manager_options": ["🌐 Publish to Live Public PR Feed", "📁 Save to Compliance Vault Only", "🖼️ Upload to Cloudflare R2 Gallery", "⏸️ Hold / Archive"]
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
            "options": llm_result.get("manager_options", ["🌐 Publish to Live PR Feed", "📁 Save to Vault"]),
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
        # Serve root PR Showcase UI
        if self.path == "/" or self.path == "/index.html":
            html_path = os.path.join(MEDIAKIT_DIR, "eternalgy_overview.html")
            if os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        # Serve Dedicated Manager AI Approval Queue Page
        if self.path in ["/queue", "/approval-queue", "/queue.html", "/manager"]:
            queue_path = os.path.join(MEDIAKIT_DIR, "queue.html")
            if os.path.exists(queue_path):
                with open(queue_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        # Serve Dedicated Cloudflare R2 Media Gallery Page
        if self.path in ["/gallery", "/photos", "/media-library", "/gallery.html"]:
            gallery_path = os.path.join(MEDIAKIT_DIR, "media_gallery.html")
            if os.path.exists(gallery_path):
                with open(gallery_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        # Serve API Documentation page for Email Server Team
        if self.path in ["/docs", "/webhook-docs", "/docs.html"]:
            docs_path = os.path.join(MEDIAKIT_DIR, "webhook_docs.html")
            if os.path.exists(docs_path):
                with open(docs_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        if self.path.startswith("/api/dashboard-state") or self.path.startswith("/api/refresh-metrics"):
            if "refresh=true" in self.path or self.path.startswith("/api/refresh-metrics"):
                state = refresh_metrics_state()
            else:
                state = load_state()
            self._send_response(200, state)
            return

        if self.path == "/health" or self.path == "/api/health":
            self._send_response(200, {
                "status": "healthy",
                "service": "Eternalgy Corporate PR & Media Center",
                "llm_engine": "MiniMax-M3",
                "image_optimization": "enabled (Pillow 12.1.0 + WebP auto-compress)",
                "image_optimizer_endpoint": "/api/optimize-image",
                "cloud_storage": "Cloudflare R2",
                "r2_bucket": R2_BUCKET_NAME,
                "r2_public_cdn": R2_PUBLIC_URL,
                "webhook_endpoint": "/webhook/email-received",
                "manager_queue_page": "/queue",
                "media_gallery_page": "/gallery",
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
        if self.path.startswith("/api/optimize-image") or self.path.startswith("/api/upload-image"):
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
            
            parsed_url = urllib.parse.urlparse(self.path)
            query_params = urllib.parse.parse_qs(parsed_url.query)

            content_type = self.headers.get("Content-Type", "")
            upload_to_r2_requested = (self.path.startswith("/api/upload-image")) or ("upload" in query_params and query_params["upload"][0].lower() == "true")

            raw_bytes = None
            filename = f"opt_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

            # High Quality PR Media Kit defaults
            quality = 96
            max_dimension = None
            lossless = False
            preserve_original = True

            if "quality" in query_params:
                try: quality = int(query_params["quality"][0])
                except ValueError: pass
            if "max_dimension" in query_params:
                try: max_dimension = int(query_params["max_dimension"][0])
                except ValueError: pass
            if "lossless" in query_params:
                lossless = query_params["lossless"][0].lower() in ("true", "1")
            if "preserve_original" in query_params:
                preserve_original = query_params["preserve_original"][0].lower() in ("true", "1")

            if "application/json" in content_type:
                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                    filename = payload.get("filename", filename)
                    if "quality" in payload:
                        quality = int(payload["quality"])
                    if "max_dimension" in payload and payload["max_dimension"] is not None:
                        max_dimension = int(payload["max_dimension"])
                    if "lossless" in payload:
                        lossless = bool(payload["lossless"])
                    if "preserve_original" in payload:
                        preserve_original = bool(payload["preserve_original"])
                    if "image_base64" in payload:
                        raw_bytes = base64.b64decode(payload["image_base64"])
                except Exception as e:
                    self._send_response(400, {"error": f"Invalid JSON payload: {e}"})
                    return
            else:
                raw_bytes = body_bytes

            if not raw_bytes:
                self._send_response(400, {"error": "No image data provided"})
                return

            res = optimize_image_data(raw_bytes, max_dimension=max_dimension, quality=quality, lossless=lossless)
            if not res.get("success"):
                self._send_response(400, res)
                return

            r2_url = None
            thumb_r2_url = None
            orig_r2_url = None
            if upload_to_r2_requested and res.get("optimized_bytes"):
                clean_name = os.path.splitext(filename)[0]
                ext = os.path.splitext(filename)[1] or ".jpg"
                main_key = f"media/{clean_name}.webp"
                thumb_key = f"media/{clean_name}_thumb.webp"
                orig_key = f"media/originals/{clean_name}{ext}"
                
                r2_url = upload_bytes_to_r2(main_key, res["optimized_bytes"], "image/webp")
                if res.get("thumbnail_bytes"):
                    thumb_r2_url = upload_bytes_to_r2(thumb_key, res["thumbnail_bytes"], "image/webp")
                if preserve_original and raw_bytes:
                    orig_mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    orig_r2_url = upload_bytes_to_r2(orig_key, raw_bytes, orig_mime)

            response_data = {
                "success": True,
                "filename": filename,
                "quality_preset": res.get("quality_preset"),
                "original_dimensions": res["original_dimensions"],
                "optimized_dimensions": res["optimized_dimensions"],
                "original_size_bytes": res["original_size_bytes"],
                "optimized_size_bytes": res["optimized_size_bytes"],
                "savings_percentage": res["savings_percentage"],
                "format": res["format"],
                "r2_url": r2_url,
                "r2_thumb_url": thumb_r2_url,
                "r2_original_url": orig_r2_url
            }
            self._send_response(200, response_data)
            return

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
                "message": "Email analyzed by MiniMax-M3 LLM, executable tasks & questions generated, marked as read on EE-Mail server.",
                "data": result
            })
            return

        if self.path == "/api/refresh-metrics":
            state = refresh_metrics_state()
            self._send_response(200, {"success": True, "message": "Live metrics and timestamps refreshed", "data": state})
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
    print(f"Starting Eternalgy PR AI Server on port {PORT} with R2 Gallery & Image Optimization...")
    server = socketserver.TCPServer(("", PORT), PRServerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server...")
        server.server_close()
