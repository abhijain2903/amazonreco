import pytest
from django.core.management import call_command
from django.test import Client


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Load the example data once; each test runs in a transaction that is rolled back."""
    with django_db_blocker.unblock():
        call_command("seed_demo", verbosity=0)


def login(username):
    from identity.models import User
    c = Client(HTTP_HX_REQUEST="true")
    c.force_login(User.objects.get(username=username))
    return c


@pytest.fixture
def as_user(db):
    return login


def toast(resp):
    import json
    return json.loads(resp.headers.get("HX-Trigger", "{}")).get("toast", {})
