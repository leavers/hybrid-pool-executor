import os

import nox
from nox import Session

os.environ.update({"PDM_IGNORE_SAVED_PYTHON": "1"})


@nox.session
def mypy(session: Session):
    session.run("pdm", "install", "-G", "mypy", external=True)
    session.run("mypy", "hybrid_pool_executor")
