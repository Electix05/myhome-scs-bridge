"""Authentication: classic OPEN nonce and HMAC SHA-256/SHA-1."""

from __future__ import annotations

import hashlib
import unittest

from myhome_scs_mqtt.app.own import auth

HMAC_A = "736F70653E"
HMAC_B = "636F70653E"


class TestOpenPassword(unittest.TestCase):
    def test_exchange_from_wissen(self):
        # WISSEN.md "Anmeldung": the MH201 sent *#898989898## and accepted
        # *#809041920##.  With the example password 12345 the algorithm
        # produces exactly that answer.
        self.assertEqual(auth.open_password("12345", "898989898"), "809041920")

    # Regression anchors computed with the same, unchanged algorithm.  They
    # only make sure the bit shuffling is never altered by accident.
    VECTORS = [
        ("12345", "603346802", "268442760"),
        ("98765", "123456789", "3823632175"),
    ]

    def test_regression_vectors(self):
        for password, nonce, expected in self.VECTORS:
            with self.subTest(password=password, nonce=nonce):
                self.assertEqual(auth.open_password(password, nonce), expected)

    def test_is_deterministic_and_depends_on_the_password(self):
        self.assertEqual(auth.open_password("12345", "603346802"),
                         auth.open_password("12345", "603346802"))
        self.assertNotEqual(auth.open_password("12345", "603346802"),
                            auth.open_password("54321", "603346802"))

    def test_answer_is_a_32_bit_number(self):
        for nonce in ("0", "1", "999999999", "8123456789", "70707070"):
            with self.subTest(nonce=nonce):
                value = int(auth.open_password("12345", nonce))
                self.assertGreaterEqual(value, 0)
                self.assertLess(value, 2 ** 32)

    def test_only_digits_are_accepted(self):
        for password in ("secret", "12a45", "", "-123", "+123", "1_000", "１２３"):
            with self.subTest(password=password):
                with self.assertRaises(ValueError):
                    auth.open_password(password, "603346802")

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(auth.open_password(" 12345 ", "603346802"),
                         auth.open_password("12345", "603346802"))


class TestDigitEncoding(unittest.TestCase):
    def test_example_from_wissen(self):
        # Every hex digit travels as a two digit decimal number: a3 -> 1003.
        self.assertEqual(auth.hex_to_digits("a3"), "1003")
        self.assertEqual(auth.digits_to_hex("1003"), "a3")

    def test_every_nibble_roundtrips(self):
        text = "0123456789abcdef"
        digits = auth.hex_to_digits(text)
        self.assertEqual(digits, "00010203040506070809101112131415")
        self.assertEqual(auth.digits_to_hex(digits), text)

    def test_a_full_digest_roundtrips(self):
        digest = hashlib.sha256(b"x").hexdigest()
        self.assertEqual(auth.digits_to_hex(auth.hex_to_digits(digest)), digest)

    def test_malformed_digit_strings_are_refused(self):
        for value in ("100", "1016", "ab", "10 3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    auth.digits_to_hex(value)
        with self.assertRaises(ValueError):
            auth.hex_to_digits("xz")


def _digits(hex_text: str) -> str:
    return "".join("%02d" % int(char, 16) for char in hex_text)


class TestHmac(unittest.TestCase):
    RA = hashlib.sha256(b"gateway nonce").hexdigest()

    def _handshake(self, algorithm: str = "sha256", password: str = "12345"):
        handshake = auth.HmacAuth(password, algorithm, random_bytes=lambda n: b"\x01" * n)
        return handshake, handshake.client_response(_digits(self.RA))

    def test_client_response_follows_the_documented_steps(self):
        _handshake, response = self._handshake()
        # Recomputed here step by step, independent of HmacAuth internals.
        rb = hashlib.sha256((b"\x01" * 32).hex().encode()).hexdigest()
        kab = hashlib.sha256(b"12345").hexdigest()
        digest = hashlib.sha256((self.RA + rb + HMAC_A + HMAC_B + kab).encode()).hexdigest()
        self.assertEqual(response, "*#%s*%s##" % (_digits(rb), _digits(digest)))

    def test_gateway_proof_is_verified(self):
        handshake, _response = self._handshake()
        rb = hashlib.sha256((b"\x01" * 32).hex().encode()).hexdigest()
        kab = hashlib.sha256(b"12345").hexdigest()
        proof = hashlib.sha256((self.RA + rb + kab).encode()).hexdigest()
        self.assertTrue(handshake.verify_server("*#%s##" % _digits(proof)))

    def test_wrong_or_malformed_proof_is_rejected(self):
        handshake, _response = self._handshake()
        wrong = hashlib.sha256(b"someone else").hexdigest()
        self.assertFalse(handshake.verify_server("*#%s##" % _digits(wrong)))
        self.assertFalse(handshake.verify_server("*#123##"))
        self.assertFalse(handshake.verify_server("*#*0##"))

    def test_no_proof_before_the_response(self):
        handshake = auth.HmacAuth("12345")
        self.assertFalse(handshake.verify_server("*#00##"))

    def test_alphanumeric_password_is_fine_for_hmac(self):
        _handshake, response = self._handshake(password="abc123XYZ")
        self.assertTrue(response.startswith("*#") and response.endswith("##"))

    def test_sha1_variant(self):
        handshake, response = self._handshake(algorithm="sha1")
        rb = hashlib.sha1((b"\x01" * 32).hex().encode()).hexdigest()
        kab = hashlib.sha1(b"12345").hexdigest()
        digest = hashlib.sha1((self.RA + rb + HMAC_A + HMAC_B + kab).encode()).hexdigest()
        self.assertEqual(response, "*#%s*%s##" % (_digits(rb), _digits(digest)))
        proof = hashlib.sha1((self.RA + rb + kab).encode()).hexdigest()
        self.assertTrue(handshake.verify_server("*#%s##" % _digits(proof)))

    def test_malformed_gateway_nonce_raises(self):
        handshake = auth.HmacAuth("12345")
        with self.assertRaises(ValueError):
            handshake.client_response("999")

    def test_unknown_algorithm_is_refused(self):
        with self.assertRaises(ValueError):
            auth.HmacAuth("12345", "md5")

    def test_client_nonce_is_random_by_default(self):
        first = auth.HmacAuth("12345").client_response(_digits(self.RA))
        second = auth.HmacAuth("12345").client_response(_digits(self.RA))
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
