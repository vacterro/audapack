"""Unit tests for SingleInstance mutex guard."""

import sys
import unittest
from audapack.single_instance import SingleInstance


class TestSingleInstance(unittest.TestCase):
    def test_first_instance_acquires_lock(self):
        inst1 = SingleInstance("TEST_SINGLE_INSTANCE_A")
        is_running1 = inst1.is_already_running()
        try:
            self.assertFalse(is_running1)
        finally:
            inst1.release()

    def test_second_instance_detected(self):
        inst1 = SingleInstance("TEST_SINGLE_INSTANCE_B")
        is_running1 = inst1.is_already_running()
        self.assertFalse(is_running1)

        inst2 = SingleInstance("TEST_SINGLE_INSTANCE_B")
        is_running2 = inst2.is_already_running()
        try:
            self.assertTrue(is_running2)
        finally:
            inst1.release()
            inst2.release()

    def test_released_instance_allows_reacquire(self):
        inst1 = SingleInstance("TEST_SINGLE_INSTANCE_C")
        self.assertFalse(inst1.is_already_running())
        inst1.release()

        inst2 = SingleInstance("TEST_SINGLE_INSTANCE_C")
        try:
            self.assertFalse(inst2.is_already_running())
        finally:
            inst2.release()
