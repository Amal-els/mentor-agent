import datetime
import uuid

from typer.testing import CliRunner

from app.cli import app
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User

runner = CliRunner()


def test_link_agenda_client_sets_an_explicit_secret():
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    uid = str(uuid.uuid4())
    session.add(User(id=uid, created_at=datetime.datetime.now(datetime.UTC)))
    session.commit()

    secret = f"secret-{uuid.uuid4().hex}"
    result = runner.invoke(
        app, ["link-agenda-client", "--user", uid, "--secret", secret]
    )

    assert result.exit_code == 0
    assert secret in result.output
    row = session.get(User, uid)
    session.refresh(row)
    assert row.agenda_client_secret == secret
    session.close()


def test_link_agenda_client_generates_a_secret_when_omitted():
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    uid = str(uuid.uuid4())
    session.add(User(id=uid, created_at=datetime.datetime.now(datetime.UTC)))
    session.commit()

    result = runner.invoke(app, ["link-agenda-client", "--user", uid])

    assert result.exit_code == 0
    row = session.get(User, uid)
    session.refresh(row)
    assert row.agenda_client_secret is not None
    # The generated secret is echoed back — the only time it's ever shown.
    assert row.agenda_client_secret in result.output
    session.close()


def test_link_agenda_client_rejects_unknown_user():
    result = runner.invoke(
        app, ["link-agenda-client", "--user", "no-such-user", "--secret", "x"]
    )

    assert result.exit_code == 1
