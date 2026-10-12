import json
import xml.etree.ElementTree as ET
from pathlib import Path

from google.protobuf.json_format import ParseDict
from livekit import api

CONFIG = Path(__file__).resolve().parents[1] / "config" / "sip"


def read(name):
    return json.loads((CONFIG / f"{name}.example.json").read_text())


def test_inbound_trunk_requires_provider_auth_and_fixed_pair():
    request = ParseDict(read("inbound-trunk"), api.CreateSIPInboundTrunkRequest())
    assert list(request.trunk.numbers) == ["<SIP_NUMBER>"]
    assert set(request.trunk.allowed_numbers) == {"+919123260733", "+916392306428"}
    response = ET.parse(CONFIG / "twilio-inbound.example.xml").getroot()
    dial = response.find("Dial")
    assert "callerId" not in dial.attrib
    assert [child.tag for child in dial] == ["Sip"]
    sip = dial.find("Sip")
    assert sip.attrib["username"] == request.trunk.auth_username
    assert sip.attrib["password"] == request.trunk.auth_password
    assert sip.text == "sip:<SIP_NUMBER>@<LIVEKIT_SIP_SUBDOMAIN>.india.sip.livekit.cloud;transport=tcp"


def test_outbound_trunk_has_india_routing_and_provider_number():
    request = ParseDict(read("outbound-trunk"), api.CreateSIPOutboundTrunkRequest())
    assert request.trunk.destination_country == "in"
    assert request.trunk.address == "<TWILIO_TERMINATION_SIP_DOMAIN>"
    assert list(request.trunk.numbers) == ["<SIP_NUMBER>"]
    assert request.trunk.auth_username and request.trunk.auth_password
    assert request.trunk.transport == api.SIP_TRANSPORT_TCP


def test_dispatch_is_bound_to_inbound_trunk_and_translator():
    request = ParseDict(read("dispatch-rule"), api.CreateSIPDispatchRuleRequest())
    rule = request.dispatch_rule
    assert list(rule.trunk_ids) == ["<LIVEKIT_INBOUND_TRUNK_ID>"]
    assert set(rule.inbound_numbers) == {"+919123260733", "+916392306428"}
    assert rule.rule.dispatch_rule_individual.room_prefix == "call-"
    assert not rule.rule.dispatch_rule_individual.no_randomness
    assert [agent.agent_name for agent in rule.room_config.agents] == ["translator"]
