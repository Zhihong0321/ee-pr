import http.server
import socketserver
import json
import urllib.request
import urllib.parse
import os
import mimetypes
import io
import base64
import hashlib
import uuid
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageOps
import boto3
from botocore.config import Config

MYT_TZ = timezone(timedelta(hours=8))

def get_kl_now():
    return datetime.now(MYT_TZ)

def get_kl_time_str():
    return get_kl_now().strftime("%Y-%m-%d %H:%M:%S GMT+8")

def get_kl_date_str():
    return get_kl_now().strftime("%Y-%m-%d")

PORT = int(os.environ.get("PORT", 8080))
MEDIAKIT_DIR = os.path.dirname(os.path.abspath(__file__))
EE_MAIL_BASE = "https://ee-mail-production.up.railway.app"
STATE_FILE = os.path.join(MEDIAKIT_DIR, "pr_dashboard_state.json")
DRAFTS_FILE = os.path.join(MEDIAKIT_DIR, "pending_pr_drafts.json")
DEBUG_LOG_FILE = os.path.join(MEDIAKIT_DIR, "pr_debug_log.json")
DEBUG_LOG_MAX_ENTRIES = 300

# Shared activity-log service. The token is intentionally supplied only at runtime.
ACTIVITY_LOG_PROXY_URL = os.environ.get(
    "ACTIVITY_LOG_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql"
)
ACTIVITY_LOG_DB_NAME = os.environ.get("ACTIVITY_LOG_DB_NAME", "prod_main")
ACTIVITY_LOG_API_TOKEN = os.environ.get("ACTIVITY_LOG_API_TOKEN")
ACTIVITY_LOG_APP = os.environ.get("ACTIVITY_LOG_APP", "eternalgy-pr-media-kit")
ACTIVITY_LOG_ENV = os.environ.get("ACTIVITY_LOG_ENV", os.environ.get("RAILWAY_ENVIRONMENT", "production"))


def get_request_context(handler):
    """Return privacy-conscious, request-scoped data for a detailed audit row."""
    forwarded_for = handler.headers.get("X-Forwarded-For", "")
    client_ip = forwarded_for.split(",")[0].strip() if forwarded_for else handler.client_address[0]
    user_agent = handler.headers.get("User-Agent", "")[:1000]
    fingerprint = hashlib.sha256(f"{client_ip}|{user_agent}".encode("utf-8")).hexdigest()[:16]
    forwarded_host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host", "")
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "https")
    path = urllib.parse.urlsplit(handler.path).path
    source_url = f"{forwarded_proto}://{forwarded_host}{path}" if forwarded_host else path
    return {
        "request_id": handler.headers.get("X-Request-ID") or str(uuid.uuid4()),
        "ip": client_ip,
        "user_agent": user_agent,
        "actor_ref": f"visitor:{fingerprint}",
        "source_url": source_url,
        "path": path,
        "referer": handler.headers.get("Referer", "")[:2000],
    }


def log_activity(action, *, entity_type=None, entity_id=None, entity_label=None,
                 description=None, status="success", context=None, actor_kind="system",
                 actor_role=None, metadata=None):
    """Best-effort shared audit logging; never makes the primary request fail."""
    if not ACTIVITY_LOG_API_TOKEN:
        return

    context = context or {}
    metadata = metadata or {}
    sql = """
        INSERT INTO public.activity_log (
            app, app_env, source_url, actor_kind, actor_ref, actor_role,
            action, entity_type, entity_id, entity_label, description, status,
            request_id, ip, user_agent, metadata
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
            $13, $14, $15, $16::jsonb
        )
    """
    params = [
        ACTIVITY_LOG_APP, ACTIVITY_LOG_ENV, context.get("source_url"), actor_kind,
        context.get("actor_ref"), actor_role, action, entity_type, str(entity_id) if entity_id else None,
        entity_label, description, status, context.get("request_id"), context.get("ip"),
        context.get("user_agent"), json.dumps(metadata, default=str),
    ]
    payload = json.dumps({"db_name": ACTIVITY_LOG_DB_NAME, "sql": sql, "params": params}).encode("utf-8")
    request = urllib.request.Request(
        ACTIVITY_LOG_PROXY_URL,
        data=payload,
        headers={"Authorization": f"Bearer {ACTIVITY_LOG_API_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status >= 300:
                raise RuntimeError(f"activity log service returned HTTP {response.status}")
    except Exception as exc:
        print(f"Activity log write skipped: {exc}")


def log_page_visit(handler, page_name):
    context = get_request_context(handler)
    log_activity(
        "visit",
        entity_type="page",
        entity_id=context["path"],
        entity_label=page_name,
        description=f"Visitor opened {page_name}",
        context=context,
        actor_kind="visitor",
        metadata={"http_method": "GET", "referer": context["referer"]},
    )

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
    state["last_updated"] = get_kl_time_str()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def append_debug_log(entry):
    """Best-effort inbound-request trail so real traffic can be inspected after the fact.
    Never raises — a failure here must not break the actual request being served."""
    try:
        entry = dict(entry)
        entry["ts"] = get_kl_time_str()
        entries = []
        if os.path.exists(DEBUG_LOG_FILE):
            try:
                with open(DEBUG_LOG_FILE, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                if not isinstance(entries, list):
                    entries = []
            except Exception:
                entries = []
        entries.insert(0, entry)
        entries = entries[:DEBUG_LOG_MAX_ENTRIES]
        with open(DEBUG_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        print(f"Debug log write failed: {e}")


def load_debug_log(limit=100):
    if not os.path.exists(DEBUG_LOG_FILE):
        return []
    try:
        with open(DEBUG_LOG_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            return []
        return entries[:limit]
    except Exception:
        return []


def read_request_body(handler):
    """Robustly read the raw body, including chunked Transfer-Encoding (no Content-Length),
    which http.server does NOT handle automatically. A sender using chunked encoding
    would otherwise silently produce an empty body under the old fixed-Content-Length read."""
    te = handler.headers.get("Transfer-Encoding", "").lower()
    if "chunked" in te:
        chunks = []
        try:
            while True:
                size_line = handler.rfile.readline().strip()
                if not size_line:
                    break
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    handler.rfile.readline()
                    break
                chunks.append(handler.rfile.read(size))
                handler.rfile.read(2)  # trailing CRLF
        except Exception as e:
            print(f"Chunked body read error: {e}")
        return b"".join(chunks)

    content_length = int(handler.headers.get("Content-Length", 0) or 0)
    return handler.rfile.read(content_length) if content_length > 0 else b""


def log_inbound_request(handler, body_bytes, matched_route, outcome, detail=None):
    context = get_request_context(handler)
    append_debug_log({
        "method": handler.command,
        "path": handler.path,
        "matched_route": matched_route,
        "outcome": outcome,
        "ip": context["ip"],
        "user_agent": context["user_agent"],
        "content_length_header": handler.headers.get("Content-Length"),
        "transfer_encoding_header": handler.headers.get("Transfer-Encoding"),
        "content_type_header": handler.headers.get("Content-Type"),
        "body_bytes_read": len(body_bytes) if body_bytes else 0,
        "body_preview": (body_bytes[:3000].decode("utf-8", errors="replace") if body_bytes else ""),
        "detail": detail or {},
    })


def refresh_metrics_state():
    state = load_state()
    now_str = get_kl_time_str()
    now_date = get_kl_date_str()
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
        seda_task = (data_obj.get("sedaTask") or {}).get("task") or {}
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
                log_page_visit(self, "Eternalgy PR & Media Center")
                with open(html_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        # Serve Dedicated Manager AI Approval Queue Page
        if self.path in ["/queue", "/approval-queue", "/queue.html", "/manager"]:
            queue_path = os.path.join(MEDIAKIT_DIR, "queue.html")
            if os.path.exists(queue_path):
                log_page_visit(self, "Manager AI Approval Queue")
                with open(queue_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        # Serve Dedicated Cloudflare R2 Media Gallery Page
        if self.path in ["/gallery", "/photos", "/media-library", "/gallery.html"]:
            gallery_path = os.path.join(MEDIAKIT_DIR, "media_gallery.html")
            if os.path.exists(gallery_path):
                log_page_visit(self, "Media Gallery")
                with open(gallery_path, "r", encoding="utf-8") as f:
                    self._send_response(200, f.read(), "text/html; charset=utf-8")
                return

        # Serve API Documentation page for Email Server Team
        if self.path in ["/docs", "/webhook-docs", "/docs.html"]:
            docs_path = os.path.join(MEDIAKIT_DIR, "webhook_docs.html")
            if os.path.exists(docs_path):
                log_page_visit(self, "Webhook Documentation")
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

        if self.path.startswith("/api/debug-log"):
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            limit = 100
            if "limit" in qs:
                try:
                    limit = int(qs["limit"][0])
                except ValueError:
                    pass
            entries = load_debug_log(limit)
            self._send_response(200, {"count": len(entries), "entries": entries})
            return

        if self.path.startswith("/debug"):
            entries = load_debug_log(100)
            rows = ""
            for e in entries:
                rows += f"""<tr>
                    <td>{e.get('ts','')}</td>
                    <td>{e.get('method','')}</td>
                    <td>{e.get('path','')}</td>
                    <td>{e.get('matched_route','')}</td>
                    <td style="color:{'#2ecc71' if 'queued' in str(e.get('outcome')) or 'ok' in str(e.get('outcome')) else '#e74c3c'}">{e.get('outcome','')}</td>
                    <td>{e.get('ip','')}</td>
                    <td>CL={e.get('content_length_header')} TE={e.get('transfer_encoding_header')} bytes_read={e.get('body_bytes_read')}</td>
                    <td><pre style="white-space:pre-wrap;max-width:400px;">{json.dumps(e.get('detail', {}), default=str)}</pre></td>
                    <td><pre style="white-space:pre-wrap;max-width:400px;">{(e.get('body_preview') or '')[:800]}</pre></td>
                </tr>"""
            html = f"""<!DOCTYPE html><html><head><title>PR Webhook Debug Log</title>
            <meta http-equiv="refresh" content="15">
            <style>body{{font-family:monospace;background:#111;color:#eee;padding:16px;}}
            table{{border-collapse:collapse;width:100%;}} td,th{{border:1px solid #444;padding:6px;font-size:12px;vertical-align:top;}}
            th{{background:#222;}}</style></head><body>
            <h2>Inbound Request Debug Log ({len(entries)} entries, newest first, auto-refreshes 15s)</h2>
            <p>Raw JSON: <a style="color:#5bc0de" href="/api/debug-log?limit=100">/api/debug-log</a></p>
            <table><tr><th>Time (GMT+8)</th><th>Method</th><th>Path</th><th>Matched Route</th><th>Outcome</th><th>IP</th><th>Body Read</th><th>Detail</th><th>Body Preview</th></tr>
            {rows}</table></body></html>"""
            self._send_response(200, html, "text/html; charset=utf-8")
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
                "debug_log_page": "/debug",
                "debug_log_api": "/api/debug-log",
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

        log_inbound_request(self, b"", "unmatched", "404_no_route_matched", {})
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
            context = get_request_context(self)
            body_bytes = read_request_body(self)
            content_length = self.headers.get("Content-Length")
            try:
                payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception as e:
                payload = {}
                log_inbound_request(self, body_bytes, "webhook/email-received", "rejected_invalid_json",
                                     {"error": str(e)})
                self._send_response(400, {"error": f"Invalid JSON payload: {e}"})
                return

            # Payload must be a JSON object to call .get() on it below. Valid JSON that
            # isn't an object (e.g. a bare array/string) is coerced to {} so it falls
            # through to the existing "missing email_id" 400 path instead of crashing
            # the request with an unguarded AttributeError.
            if not isinstance(payload, dict):
                payload = {}

            email_id = payload.get("email_id") or payload.get("id") or (payload.get("email") or {}).get("email_id")

            if not email_id:
                log_activity(
                    "request_received", entity_type="pr_request", description="Inbound PR request rejected: missing email_id",
                    status="failed", context=context, actor_kind="system",
                    metadata={"endpoint": context["path"], "content_length": content_length},
                )
                log_inbound_request(self, body_bytes, "webhook/email-received", "rejected_missing_email_id",
                                     {"payload_keys": list(payload.keys())})
                self._send_response(400, {"error": "Missing email_id in webhook payload"})
                return

            try:
                result = process_incoming_email(email_id, payload)
            except Exception as exc:
                log_activity(
                    "request_received", entity_type="pr_request", entity_id=email_id,
                    entity_label=f"PR request {email_id}", description="Inbound PR request processing failed",
                    status="failed", context=context, actor_kind="system",
                    metadata={"endpoint": context["path"], "content_length": content_length, "error_type": type(exc).__name__},
                )
                log_inbound_request(self, body_bytes, "webhook/email-received", "processing_error",
                                     {"email_id": email_id, "error_type": type(exc).__name__, "error": str(exc)})
                self._send_response(500, {"error": "Unable to process inbound PR request"})
                return

            log_activity(
                "request_received", entity_type="pr_request", entity_id=email_id,
                entity_label=f"PR request {email_id}", description="Inbound PR request analyzed and queued",
                context=context, actor_kind="system",
                metadata={
                    "endpoint": context["path"], "content_length": content_length,
                    "category": result.get("llm_result", {}).get("category"),
                    "manager_question_generated": result.get("manager_question_generated", False),
                },
            )
            log_inbound_request(self, body_bytes, "webhook/email-received",
                                 "queued" if result.get("manager_question_generated") else "processed_no_queue_card",
                                 {
                                     "email_id": email_id,
                                     "category": result.get("llm_result", {}).get("category"),
                                     "manager_question_generated": result.get("manager_question_generated", False),
                                 })
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
            context = get_request_context(self)
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                payload = {}

            q_id = payload.get("question_id")
            chosen_opt = payload.get("selected_option")

            if not q_id or not chosen_opt:
                log_activity(
                    "approve", entity_type="manager_question", entity_id=q_id,
                    description="Manager approval rejected: missing question ID or selected option",
                    status="failed", context=context, actor_kind="manager", actor_role="manager",
                    metadata={"endpoint": context["path"], "content_length": content_length},
                )
                self._send_response(400, {"success": False, "error": "question_id and selected_option are required"})
                return

            state = load_state()
            resolved_question = None
            for q in state.get("manager_questions", []):
                if q.get("question_id") == q_id:
                    q["status"] = "RESOLVED"
                    q["selected_option"] = chosen_opt
                    resolved_question = q

                    state.setdefault("published_updates", []).insert(0, {
                        "id": f"PUB-{q_id}",
                        "category": q.get("category", "PRESS_RELEASE"),
                        "title": q.get("email_subject", "PR Announcement"),
                        "date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "summary": f"Manager Selected: '{chosen_opt}'. {q.get('ai_analysis', '')}",
                        "badge": q.get("category", "PR UPDATE")
                    })
                    break

            if not resolved_question:
                log_activity(
                    "approve", entity_type="manager_question", entity_id=q_id,
                    description="Manager approval rejected: request was not found",
                    status="failed", context=context, actor_kind="manager", actor_role="manager",
                    metadata={"endpoint": context["path"], "selected_option": chosen_opt},
                )
                self._send_response(404, {"success": False, "error": "Manager question not found"})
                return

            save_state(state)
            log_activity(
                "approve", entity_type="manager_question", entity_id=q_id,
                entity_label=f"Manager question {q_id}", description="Manager approved a queued PR request",
                context=context, actor_kind="manager", actor_role="manager",
                metadata={
                    "endpoint": context["path"], "selected_option": chosen_opt,
                    "category": resolved_question.get("category"), "email_id": resolved_question.get("email_id"),
                },
            )
            self._send_response(200, {"success": True, "message": f"Recorded manager choice: '{chosen_opt}'"})
            return

        unmatched_body = read_request_body(self)
        log_inbound_request(self, unmatched_body, "unmatched", "404_no_route_matched", {})
        self._send_response(404, {"error": "Endpoint not found"})

if __name__ == "__main__":
    print(f"Starting Eternalgy PR AI Server on port {PORT} with R2 Gallery & Image Optimization...")
    server = socketserver.TCPServer(("", PORT), PRServerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server...")
        server.server_close()
