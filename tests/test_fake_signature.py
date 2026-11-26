"""Pin the fake's ``updateStateOnServer`` to the real Indigo signature.

The string below was read live from Indigo 2026.1 on 2026-11-26 with
``indigo.Device.updateStateOnServer.__doc__``. The THIRD POSITIONAL is ``decimalPlaces``;
an earlier fake put ``clearErrorState`` third, which hid a bug where every "keep the error
flag" write cleared it (a positional ``False`` lands in decimalPlaces). Do not "tidy" the
fake back to the nicer order.
"""
from __future__ import annotations

import inspect
import re

from indigo_fakes import FakeDev

REAL_DOC_SIGNATURE = (
    "updateStateOnServer(key, value [, decimalPlaces=-1 [, triggerEvents=True "
    "[, clearErrorState=True [, uiValue=None]]]])")


def _real_parameters():
    inner = REAL_DOC_SIGNATURE[REAL_DOC_SIGNATURE.index("(") + 1:-1]
    names = re.findall(r"(\w+)(?:=([^\s\[\],]+))?", inner)
    return [(n, d) for n, d in names]


def test_the_fake_has_exactly_the_real_parameters_in_the_real_order():
    real = _real_parameters()
    assert [n for n, _ in real] == ["key", "value", "decimalPlaces", "triggerEvents",
                                    "clearErrorState", "uiValue"]
    params = list(inspect.signature(FakeDev.updateStateOnServer).parameters.values())[1:]   # drop self
    assert [p.name for p in params] == [n for n, _ in real]
    for param, (_name, default) in zip(params, real):
        if default == "":
            assert param.default is inspect.Parameter.empty
        else:
            assert repr(param.default) == default


def test_a_positional_third_argument_is_decimal_places_not_the_error_flag():
    dev = FakeDev(1, "d", "matterLock", {})
    dev.setErrorStateOnServer("unreachable")
    dev.updateStateOnServer("lockState", "locked", False)          # what the bug did
    assert dev.single_key_calls[-1]["decimalPlaces"] is False
    assert dev.single_key_calls[-1]["clearErrorState"] is True
    assert dev.errorState == ""                                    # i.e. it cleared the flag
