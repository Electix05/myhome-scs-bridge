"""Frame parsing, frame builders and value encodings - no network involved."""

from __future__ import annotations

import dataclasses
import random
import unittest

from myhome_scs_mqtt.app.own import messages as msg


class TestParse(unittest.TestCase):
    def test_every_frame_shape_of_the_table(self):
        cases = {
            "*#*1##": msg.KIND_ACK,
            "*#*0##": msg.KIND_NACK,
            "*1*1*12##": msg.KIND_NORMAL,
            "*#1*12##": msg.KIND_STATUS_REQUEST,
            "*#4*2*0##": msg.KIND_DIMENSION_REQUEST,
            "*#4*2*0*0215##": msg.KIND_DIMENSION_RESPONSE,
            "*#4*#2*#14*0215*3##": msg.KIND_DIMENSION_WRITE,
        }
        for frame, kind in cases.items():
            with self.subTest(frame=frame):
                self.assertEqual(msg.parse(frame).kind, kind)

    def test_ack_and_nack_terminate_an_exchange(self):
        self.assertTrue(msg.parse("*#*1##").is_ack)
        self.assertTrue(msg.parse("*#*0##").is_nack)
        self.assertTrue(msg.parse("*#*0##").is_reply)
        self.assertFalse(msg.parse("*1*1*12##").is_reply)

    def test_heating_zone_answers_from_wissen(self):
        temperature = msg.parse("*#4*1*0*0243##")
        self.assertEqual((temperature.who, temperature.where, temperature.dimension),
                         ("4", "1", "0"))
        self.assertEqual(temperature.values, ("0243",))

        setpoint = msg.parse("*#4*1*12*0210*3##")
        self.assertEqual(setpoint.dimension, "12")
        self.assertEqual(setpoint.values, ("0210", "3"))

        zone_off = msg.parse("*4*303*1##")
        self.assertEqual((zone_off.kind, zone_off.what, zone_off.where),
                         (msg.KIND_NORMAL, "303", "1"))

    def test_actuator_addresses_keep_their_suffix(self):
        # Zone actuator "8#1" and the pump at the central unit "0#3".
        actuator = msg.parse("*#4*8#1*20*8##")
        self.assertEqual(actuator.where, "8")
        self.assertEqual(actuator.where_params, ("1",))
        self.assertEqual(actuator.full_where, "8#1")
        self.assertEqual(actuator.values, ("8",))

        pump = msg.parse("*#4*0#3*20*1##")
        self.assertEqual(pump.full_where, "0#3")

        # The central unit writes to the actuator - a write, not a report.
        written = msg.parse("*#4*8#1*#20*8##")
        self.assertEqual(written.kind, msg.KIND_DIMENSION_WRITE)
        self.assertEqual((written.full_where, written.dimension), ("8#1", "20"))

    def test_zone_command_forms(self):
        central = msg.parse("*4*303*#8##")
        self.assertEqual(central.where, "#8")
        self.assertEqual(central.where_params, ())
        standalone = msg.parse("*4*303*8##")
        self.assertEqual(standalone.where, "8")

    def test_bus_suffix_and_group_prefix(self):
        message = msg.parse("*2*1*41#4#01##")
        self.assertEqual(message.where, "41")
        self.assertEqual(message.where_params, ("4", "01"))
        self.assertEqual(message.bus, "#4#01")
        self.assertEqual(message.full_where, "41#4#01")
        group = msg.parse("*1*0*#3##")
        self.assertEqual(group.where, "#3")
        self.assertEqual(group.bus, "")

    def test_addresses_stay_strings(self):
        # "04" is not the number 4.
        message = msg.parse("*#2*04*10*10*50*001*0##")
        self.assertEqual(message.where, "04")
        self.assertIsInstance(message.where, str)

    def test_shutter_status_and_position_command(self):
        status = msg.parse("*#2*05*10*11*27*001*0##")
        self.assertEqual(status.kind, msg.KIND_DIMENSION_RESPONSE)
        self.assertEqual(status.dimension, "10")
        self.assertEqual(status.values, ("11", "27", "001", "0"))

        goto = msg.parse("*#2*05*#11#001#1*50##")
        self.assertEqual(goto.kind, msg.KIND_DIMENSION_WRITE)
        self.assertEqual(goto.dimension, "11#001#1")
        self.assertEqual(goto.values, ("50",))

    def test_cen_and_cen_plus_parameters(self):
        cen_plus = msg.parse("*25*21#2*22##")
        self.assertEqual((cen_plus.who, cen_plus.what, cen_plus.what_params),
                         ("25", "21", ("2",)))
        cen = msg.parse("*15*3#2*51##")
        self.assertEqual((cen.what, cen.what_params, cen.where), ("3", ("2",), "51"))

    def test_gateway_frames_with_an_empty_address(self):
        request = msg.parse("*#13**15##")
        self.assertEqual((request.kind, request.who, request.where, request.dimension),
                         (msg.KIND_DIMENSION_REQUEST, "13", "", "15"))
        clock = msg.parse("*#13**0*21*11*44*999##")
        self.assertEqual(clock.kind, msg.KIND_DIMENSION_RESPONSE)
        self.assertEqual(clock.values, ("21", "11", "44", "999"))

    def test_handshake_frames_are_other(self):
        for frame in ("*#898989898##", "*98*2##", "*98*1##"):
            with self.subTest(frame=frame):
                self.assertEqual(msg.parse(frame).kind, msg.KIND_OTHER)
        # WISSEN.md "Anmeldung": the F461 asked for SHA-256 with *98*2##.
        self.assertEqual(msg.HMAC_SHA256_CHALLENGE, "*98*2##")
        self.assertEqual(msg.HMAC_SHA1_CHALLENGE, "*98*1##")

    def test_light_management_sensor_answers(self):
        lux = msg.parse("*#1*1010*6*220##")
        self.assertEqual((lux.dimension, lux.values), ("6", ("220",)))
        presence = msg.parse("*#1*1010*7*0*0*2##")
        self.assertEqual(presence.values, ("0", "0", "2"))

    def test_garbage_never_raises(self):
        for frame in ("", "*", "##", "*##", "**##", "hello", "*1*1##", "*###",
                      "*#1##", "#*1*1*12##", "*1*1*12#"):
            with self.subTest(frame=frame):
                self.assertEqual(msg.parse(frame).kind, msg.KIND_OTHER)

        alphabet = "*#0123456789ab "
        generator = random.Random(4711)
        for _ in range(2000):
            frame = "".join(generator.choice(alphabet)
                            for _ in range(generator.randint(0, 20)))
            msg.parse(frame)                    # must not raise

    def test_messages_are_immutable(self):
        message = msg.parse("*1*1*12##")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            message.what = "0"  # type: ignore[misc]


class TestSplitFrames(unittest.TestCase):
    def test_several_frames_and_an_incomplete_rest(self):
        frames, tail = msg.split_frames("*1*1*12##*1*0*13##*1*")
        self.assertEqual(frames, ["*1*1*12##", "*1*0*13##"])
        self.assertEqual(tail, "*1*")

    def test_noise_in_front_of_a_frame_is_dropped(self):
        frames, tail = msg.split_frames("\r\nxx*#*1##")
        self.assertEqual(frames, ["*#*1##"])
        self.assertEqual(tail, "")

    def test_a_frame_split_over_two_chunks(self):
        frames, tail = msg.split_frames("*#4*1*0*02")
        self.assertEqual((frames, tail), ([], "*#4*1*0*02"))
        frames, tail = msg.split_frames(tail + "43##")
        self.assertEqual((frames, tail), (["*#4*1*0*0243##"], ""))

    def test_empty_buffer(self):
        self.assertEqual(msg.split_frames(""), ([], ""))


class TestBuilders(unittest.TestCase):
    def test_basic_builders(self):
        self.assertEqual(msg.command("1", "1", "12"), "*1*1*12##")
        self.assertEqual(msg.status_request("1", "0"), "*#1*0##")
        self.assertEqual(msg.dimension_request("18", "51", "113"), "*#18*51*113##")
        self.assertEqual(
            msg.dimension_write("4", "#2", "14", "0215", "3"), "*#4*#2*#14*0215*3##"
        )

    def test_position_command_has_the_measured_form(self):
        # WISSEN.md: exactly this form is accepted, five others are refused.
        self.assertEqual(msg.dimension_write("2", "05", "11#001#1", "50"),
                         "*#2*05*#11#001#1*50##")

    def test_fan_speed_without_hash_in_front_of_the_zone(self):
        self.assertEqual(msg.dimension_write("4", "8", "11", "1"), "*#4*8*#11*1##")

    def test_gateway_requests(self):
        self.assertEqual(
            msg.dimension_request(msg.WHO_GATEWAY, "", msg.DIM_GATEWAY_TIME), "*#13**0##")
        self.assertEqual(
            msg.dimension_request(msg.WHO_GATEWAY, "", msg.DIM_GATEWAY_FIRMWARE),
            "*#13**16##")


class TestValues(unittest.TestCase):
    def test_temperature_roundtrip(self):
        self.assertEqual(msg.encode_temperature(21.5), "0215")
        self.assertEqual(msg.encode_temperature(24.3), "0243")
        self.assertEqual(msg.encode_temperature(-3), "1030")
        self.assertEqual(msg.decode_temperature("0243"), 24.3)
        self.assertEqual(msg.decode_temperature("0290"), 29.0)
        self.assertEqual(msg.decode_temperature("1030"), -3.0)
        for value in (-12.5, 0.0, 7.0, 21.0, 35.0):
            with self.subTest(value=value):
                self.assertEqual(msg.decode_temperature(msg.encode_temperature(value)),
                                 value)

    def test_temperature_limits_and_garbage(self):
        self.assertEqual(msg.encode_temperature(120), "0999")
        self.assertEqual(msg.encode_temperature(21.04), "0210")
        self.assertIsNone(msg.decode_temperature("abc"))
        self.assertIsNone(msg.decode_temperature("00"))     # dimension 13, unknown
        self.assertIsNone(msg.decode_temperature(""))
        # "²".isdigit() is True, int() would raise - must be None, not a crash.
        self.assertIsNone(msg.decode_temperature("0²43"))

    def test_dali_level_from_wissen(self):
        # *#1*29*#1*175*2##  ->  ACK, answer *#1*29*1*175*2##  = 75 %
        answer = msg.parse("*#1*29*1*175*2##")
        self.assertEqual(answer.dimension, "1")
        self.assertEqual(msg.decode_level(answer.values[0]), 75)
        # Switched off, the lamp answers in dimension 4 with the same encoding.
        dark = msg.parse("*#1*29*4*100*2##")
        self.assertEqual((dark.dimension, msg.decode_level(dark.values[0])), ("4", 0))
        write = msg.parse("*#1*29*#1*101*2##")
        self.assertEqual(msg.decode_level(write.values[0]), 1)

    def test_level_encoding(self):
        self.assertEqual(msg.encode_level(75), "175")
        self.assertEqual(msg.encode_level(0), "100")
        self.assertEqual(msg.encode_level(140), "200")
        self.assertEqual(msg.decode_level("200"), 100)
        self.assertEqual(msg.decode_level("100"), 0)
        self.assertIsNone(msg.decode_level("99"))
        self.assertIsNone(msg.decode_level("201"))
        self.assertIsNone(msg.decode_level("x"))
        self.assertIsNone(msg.decode_level("1²5"))


class TestColourValues(unittest.TestCase):
    """Dimension 12 and 14 of a DALI lamp.

    Every frame in here was recorded at the installation on 19.09.2026, see
    berichte/17-messung-dali-farbe.md and berichte/18-messung-dali-schreiben.md.
    """

    def test_the_three_colours_that_were_recorded(self):
        for raw, hsv in (("*#1*0211*12*243*96*100##", (243, 96, 100)),    # blue
                         ("*#1*0211*12*352*70*100##", (352, 70, 100)),    # red
                         ("*#1*0211*12*120*98*100##", (120, 98, 100))):   # green
            with self.subTest(frame=raw):
                answer = msg.parse(raw)
                self.assertEqual(answer.dimension, msg.DIM_COLOR_HSV)
                self.assertEqual(msg.decode_hsv(answer.values), hsv)

    def test_the_colour_command_has_the_measured_form(self):
        # Sent at the installation, the lamp turned green and confirmed it.
        values = msg.encode_hsv(120, 100)
        self.assertEqual(values, ("120", "100", "100"))
        self.assertEqual(msg.dimension_write("1", "0211", msg.DIM_COLOR_HSV, *values),
                         "*#1*0211*#12*120*100*100##")

    def test_colour_limits(self):
        self.assertEqual(msg.encode_hsv(360, 150), ("0", "100", "100"))   # 360 = 0
        self.assertEqual(msg.encode_hsv(359.6, -5), ("0", "0", "100"))    # rounds to 360
        self.assertEqual(msg.encode_hsv(0, 0, 0), ("0", "0", "0"))
        self.assertEqual(msg.encode_hsv("243", "96"), ("243", "96", "100"))

    def test_nonsense_is_no_colour(self):
        for bad in (("rot", 50), (None, 50), (120, None), (120, [1]),
                    (float("nan"), 1), (float("inf"), 1)):
            with self.subTest(values=bad):
                self.assertIsNone(msg.encode_hsv(*bad))
        for values in (("243", "96"), (), ("243", "96", "101"), ("360", "0", "100"),
                       ("24a", "96", "100"), ("2²3", "96", "100"), ("", "", "")):
            with self.subTest(values=values):
                self.assertIsNone(msg.decode_hsv(values))

    def test_white_temperature_is_mired_times_hundred(self):
        answer = msg.parse("*#1*29*14*38528##")
        self.assertEqual(answer.dimension, msg.DIM_WHITE_TEMP)
        # Confirmed at the installation: 2700 K was sent as 37040
        # (370.4 mired x 100) and the lamp took it.
        self.assertEqual(msg.decode_white("37040"), 2700)
        # The table of berichte/18-messung-dali-schreiben.md, our direction.
        self.assertEqual(msg.encode_white(2000), "50000")
        self.assertEqual(msg.encode_white(4000), "25000")
        self.assertEqual(msg.encode_white(6200), "16129")
        # 2700 K comes out as 37037: the hand written 37040 was rounded to one
        # decimal mired first.  0.2 K apart, and it reads back as 2700 K.
        self.assertEqual(msg.encode_white(2700), "37037")
        self.assertEqual(msg.decode_white("37037"), 2700)

    def test_the_white_command_has_the_measured_form(self):
        self.assertEqual(msg.dimension_write("1", "29", msg.DIM_WHITE_TEMP, "38528"),
                         "*#1*29*#14*38528##")

    def test_the_ends_the_lamp_reported(self):
        # Sent 945 and 65534, the lamp answered with its own limits.
        self.assertEqual(msg.decode_white("945"), 105820)
        self.assertEqual(msg.decode_white("65534"), 1526)

    def test_kelvin_survives_the_round_trip(self):
        for kelvin in range(2000, 6300, 100):
            with self.subTest(kelvin=kelvin):
                value = msg.encode_white(kelvin)
                self.assertIsNotNone(value)
                self.assertAlmostEqual(msg.decode_white(value), kelvin, delta=1)

    def test_nonsense_is_no_white_temperature(self):
        for bad in ("0", "-5", "", "abc", "3²0", " "):
            with self.subTest(value=bad):
                self.assertIsNone(msg.decode_white(bad))
        for bad in (0, -5, None, "warm", float("inf"), True):
            with self.subTest(value=bad):
                self.assertIsNone(msg.encode_white(bad))


if __name__ == "__main__":
    unittest.main()
