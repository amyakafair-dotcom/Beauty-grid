"""Customer-chat assistant on WhatsApp, Instagram and Facebook.

Meta posts every customer message to webhook(). Claude answers from the company's own data
(read-only tools). It never takes orders, and it goes quiet once it hands the chat to a person.
"""
import hashlib
import hmac
import json
import threading
import urllib.request

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt

from . import services
from .models import Channel, Chat, ChatMessage, Product

GRAPH = "https://graph.facebook.com/v21.0"

SYSTEM = (
    "You are the customer assistant of {company}, a cosmetics brand. Reply in the customer's "
    "language (Arabic by default), short and warm, plain text only. Use the tools for every "
    "price, product and stock fact; never guess them. You cannot take orders or payments: "
    "give the store link or say a team member will confirm. For skin conditions, allergies, "
    "complaints or anything you cannot answer from the tools, call handoff_to_human and tell "
    "the customer a person will reply soon."
)

TOOLS = [
    {"name": "search_products", "description": "Find sellable products by name; returns price and availability.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_store_link", "description": "The online store address to give the customer.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "handoff_to_human", "description": "Stop answering and flag this chat for a person.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]


def signature_ok(body, header):
    secret = settings.META_APP_SECRET.encode()
    if not secret:
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def parse(payload):
    """Text messages as (channel kind, our Meta ID, sender, text, message id)."""
    out = []
    kinds = {"instagram": Channel.INSTAGRAM, "page": Channel.FACEBOOK}
    for entry in payload.get("entry", []):
        if payload.get("object") == "whatsapp_business_account":
            for change in entry.get("changes", []):
                v = change.get("value", {})
                for msg in v.get("messages", []):
                    if msg.get("type") == "text":
                        out.append((Channel.WHATSAPP, v.get("metadata", {}).get("phone_number_id", ""),
                                    msg["from"], msg["text"]["body"], msg.get("id", "")))
        elif payload.get("object") in kinds:
            for ev in entry.get("messaging", []):
                m = ev.get("message", {})
                if m.get("text") and not m.get("is_echo"):
                    out.append((kinds[payload["object"]], ev["recipient"]["id"], ev["sender"]["id"],
                                m["text"], m.get("mid", "")))
    return out


def run_tool(name, args, chat):
    company = chat.company
    if name == "search_products":
        from .store import _available, _products
        found = _products(company).filter(name__icontains=args.get("query", ""))[:6]
        rows = []
        for p in found:
            qty = _available(p, company)
            rows.append({"name": str(p), "price": str(p.sale_price), "currency": company.currency,
                         "in_stock": qty is None or qty > 0})
        return json.dumps(rows, ensure_ascii=False) if rows else "No matching product."
    if name == "get_store_link":
        from .admin import store_url
        return store_url(company)
    if name == "handoff_to_human":
        chat.human = True
        chat.save(update_fields=["human"])
        return "Chat handed to a person."
    return "Unknown tool."


def ask_claude(chat):
    import anthropic
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    msgs = [{"role": m.role, "content": m.text} for m in chat.messages.order_by("-pk")[:12][::-1]]
    for _ in range(4):
        r = client.messages.create(model=settings.ASSISTANT_MODEL, max_tokens=500,
                                   system=SYSTEM.format(company=chat.company.name),
                                   tools=TOOLS, messages=msgs)
        if r.stop_reason != "tool_use":
            return "".join(b.text for b in r.content if b.type == "text").strip()
        msgs.append({"role": "assistant", "content": r.content})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.id, "content": run_tool(b.name, b.input, chat)}
            for b in r.content if b.type == "tool_use"]})
    return ""


def send(channel, to, text):
    if channel.kind == Channel.WHATSAPP:
        url = f"{GRAPH}/{channel.external_id}/messages"
        body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    else:
        url = f"{GRAPH}/{channel.external_id}/messages"
        body = {"recipient": {"id": to}, "message": {"text": text}, "messaging_type": "RESPONSE"}
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Authorization": f"Bearer {settings.META_ACCESS_TOKEN}"})
    urllib.request.urlopen(req, timeout=10).close()


def process(payload):
    for kind, ours, sender, text, mid in parse(payload):
        channel = Channel.objects.filter(kind=kind, external_id=ours).select_related("company").first()
        if not channel or not channel.company.active:
            continue
        if mid and ChatMessage.objects.filter(chat__channel=channel, mid=mid).exists():
            continue  # Meta retried
        chat, _ = Chat.objects.get_or_create(company=channel.company, channel=channel, contact=sender)
        chat.messages.create(role="user", text=text, mid=mid)
        if chat.human:
            continue
        try:
            answer = ask_claude(chat)
            if answer:
                chat.messages.create(role="assistant", text=answer)
                send(channel, sender, answer)
        except Exception:
            chat.human = True  # never leave a customer unanswered by a broken bot: a person takes over
            chat.save(update_fields=["human"])


@csrf_exempt
def webhook(request):
    if request.method == "GET":  # Meta's one-time check when you save the webhook URL
        ok = (request.GET.get("hub.mode") == "subscribe" and settings.META_VERIFY_TOKEN
              and request.GET.get("hub.verify_token") == settings.META_VERIFY_TOKEN)
        return HttpResponse(request.GET.get("hub.challenge", ""), status=200) if ok else HttpResponseForbidden()
    if request.method != "POST" or not signature_ok(request.body, request.headers.get("X-Hub-Signature-256")):
        return HttpResponseForbidden()
    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    threading.Thread(target=process, args=(payload,), daemon=True).start()  # Meta wants 200 within 20 s
    return HttpResponse("ok")
