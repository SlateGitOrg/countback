from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from countback import generator
from countback.aging import Ledger
from countback.dso import build_metrics

SEED = 7
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("data")
    generator.write(generator.generate(SEED), out)
    return out


@pytest.fixture(scope="session")
def ledger(data_dir) -> Ledger:
    return Ledger(data_dir / "invoices.csv")


@pytest.fixture(scope="session")
def metrics(ledger) -> pd.DataFrame:
    return build_metrics(ledger)


@pytest.fixture(scope="session")
def truth(data_dir) -> dict:
    return json.loads((data_dir / "truth.json").read_text())


@pytest.fixture(scope="session")
def undisputed_metrics(tmp_path_factory) -> pd.DataFrame:
    out = tmp_path_factory.mktemp("undisputed")
    generator.write(generator.generate(SEED, dispute=False), out)
    return build_metrics(Ledger(out / "invoices.csv"))
