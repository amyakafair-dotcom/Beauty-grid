import hashlib
import hmac
import json
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from . import assistant, services
from .models import Channel, Chat, Partner, Product, PurchaseOrder


def wa(text, mid="m1", sender="249911", ours="PN1"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": ours},
        "messages": [{"from": sender, "id": mid, "type": "text", "text": {"body": text}}]}}]}]}


def ig(text, mid="i1", sender="u1", ours="IG1", echo=False):
    m = {"mid": mid, "text": text}
    if echo:
        m["is_echo"] = True
    return {"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": sender}, "recipient": {"id": ours}, "message": m}]}]}


@override_settings(META_APP_SECRET="s3cret", META_VERIFY_TOKEN="vt", META_ACCESS_TOKEN="tok")
class Assistant(TestCase):
    def setUp(self):
        self.client.post("/signup/", {"company": "Indigo", "username": "owner", "password": "secret-pass-1"})
        self.co = User.objects.get(username="owner").profile.company
        self.channel = Channel.objects.create(company=self.co, kind="whatsapp", external_id="PN1")
        Channel.objects.create(company=self.co, kind="instagram", external_id="IG1")
        self.aura = Product.objects.create(company=self.co, name="Aura", sale_price=D("8000"), published=True)
        wh = self.co.warehouse_set.first()
        po = PurchaseOrder.objects.create(company=self.co, partner=Partner.objects.create(
            company=self.co, name="V", is_vendor=True), warehouse=wh)
        po.lines.create(product=self.aura, quantity=5, price=1)
        services.receive_purchase(po)

    def post(self, payload, secret="s3cret"):
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with mock.patch("erp.assistant.threading.Thread") as t:
            r = self.client.post("/webhooks/meta/", body, content_type="application/json",
                                 headers={"X-Hub-Signature-256": sig})
        return r, t

    def test_verification_handshake(self):
        ok = self.client.get("/webhooks/meta/", {"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
        self.assertEqual((ok.status_code, ok.content), (200, b"42"))
        self.assertEqual(self.client.get("/webhooks/meta/", {"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"}).status_code, 403)

    def test_bad_signature_is_refused_and_nothing_runs(self):
        r, t = self.post(wa("hi"), secret="wrong")
        self.assertEqual(r.status_code, 403)
        t.assert_not_called()

    def test_good_signature_returns_200_and_works_in_background(self):
        r, t = self.post(wa("hi"))
        self.assertEqual(r.status_code, 200)
        t.return_value.start.assert_called_once()

    def test_message_gets_stored_answered_and_sent(self):
        with mock.patch("erp.assistant.ask_claude", return_value="أهلاً") as ask, \
                mock.patch("erp.assistant.send") as send:
            assistant.process(wa("كم سعر Aura؟"))
        chat = Chat.objects.get()
        self.assertEqual([m.role for m in chat.messages.all()], ["user", "assistant"])
        send.assert_called_once_with(self.channel, "249911", "أهلاً")
        ask.assert_called_once()

    def test_meta_retry_does_not_answer_twice(self):
        with mock.patch("erp.assistant.ask_claude", return_value="x"), mock.patch("erp.assistant.send") as send:
            assistant.process(wa("hi", mid="same"))
            assistant.process(wa("hi", mid="same"))
        self.assertEqual(send.call_count, 1)

    def test_instagram_parsed_and_echo_ignored(self):
        with mock.patch("erp.assistant.ask_claude", return_value="hello"), mock.patch("erp.assistant.send") as send:
            assistant.process(ig("hi"))
            assistant.process(ig("our own reply", mid="e1", echo=True))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(Chat.objects.get().channel.kind, "instagram")

    def test_unknown_number_is_ignored(self):
        with mock.patch("erp.assistant.ask_claude") as ask:
            assistant.process(wa("hi", ours="SOMEONE-ELSE"))
        ask.assert_not_called()
        self.assertEqual(Chat.objects.count(), 0)

    def test_handed_over_chat_stays_silent(self):
        with mock.patch("erp.assistant.ask_claude", return_value="x"), mock.patch("erp.assistant.send") as send:
            assistant.process(wa("hi", mid="1"))
            Chat.objects.update(human=True)
            assistant.process(wa("anyone?", mid="2"))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(Chat.objects.get().messages.filter(role="user").count(), 2)

    def test_failure_hands_chat_to_a_person(self):
        with mock.patch("erp.assistant.ask_claude", side_effect=RuntimeError), mock.patch("erp.assistant.send") as send:
            assistant.process(wa("hi"))
        send.assert_not_called()
        self.assertTrue(Chat.objects.get().human)

    def test_tools_read_only_and_only_this_company(self):
        self.client.post("/signup/", {"company": "Other", "username": "o3", "password": "secret-pass-1"})
        Product.objects.create(company=User.objects.get(username="o3").profile.company, name="Aura Secret",
                               sale_price=D("1"), published=True)
        chat = Chat.objects.create(company=self.co, channel=self.channel, contact="c")
        out = json.loads(assistant.run_tool("search_products", {"query": "aura"}, chat))
        self.assertEqual([r["name"] for r in out], ["Aura"])
        self.assertTrue(out[0]["in_stock"])
        self.assertEqual(assistant.run_tool("search_products", {"query": "zzz"}, chat), "No matching product.")
        assistant.run_tool("handoff_to_human", {"reason": "allergy"}, chat)
        chat.refresh_from_db()
        self.assertTrue(chat.human)

    def test_claude_tool_loop_calls_tool_then_answers(self):
        chat = Chat.objects.create(company=self.co, channel=self.channel, contact="c")
        chat.messages.create(role="user", text="سعر Aura؟")
        block = mock.Mock(type="tool_use", id="t1", input={"query": "Aura"})
        block.name = "search_products"
        first = mock.Mock(stop_reason="tool_use", content=[block])
        final = mock.Mock(stop_reason="end_turn", content=[mock.Mock(type="text", text="8000 ج.س")])
        client = mock.Mock()
        client.messages.create.side_effect = [first, final]
        with mock.patch("anthropic.Anthropic", return_value=client):
            self.assertEqual(assistant.ask_claude(chat), "8000 ج.س")
        result = client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"][0]
        self.assertEqual(result["tool_use_id"], "t1")
        self.assertIn("Aura", result["content"])
