import unittest

from app.routing import lookup

A, B = "+919123260733", "+916392306428"
PARTIES = {A: "od-IN", B: "hi-IN"}


class RoutingTests(unittest.TestCase):
    def test_odia_phone_can_call_hindi_phone(self):
        self.assertEqual(lookup(A, PARTIES), {
            "caller_lang": "od-IN", "other_no": B, "other_lang": "hi-IN",
        })

    def test_hindi_phone_can_call_odia_phone(self):
        self.assertEqual(lookup(B, PARTIES), {
            "caller_lang": "hi-IN", "other_no": A, "other_lang": "od-IN",
        })

    def test_unknown_callers_cannot_trigger_outbound_calls(self):
        for caller in ("+919999999999", "+918328878286", "", "9123260733", A + " ", "' OR 1=1 --"):
            with self.subTest(caller=caller):
                self.assertIsNone(lookup(caller, PARTIES))

    def test_repeated_calls_do_not_need_a_lease_or_order(self):
        for _ in range(3):
            self.assertEqual(lookup(A, PARTIES)["other_no"], B)
            self.assertEqual(lookup(B, PARTIES)["other_no"], A)
        self.assertEqual(PARTIES, {A: "od-IN", B: "hi-IN"})

    def test_invalid_pair_size_is_rejected(self):
        for parties in ({}, {A: "od-IN"}, {**PARTIES, "+919999999999": "en-IN"}):
            with self.subTest(parties=parties), self.assertRaises(ValueError):
                lookup(A, parties)
