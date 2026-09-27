"""Response helpers for HTMX commands."""
import json

from django.http import HttpResponse
from django.shortcuts import render


def done(request, toast=None, tone="ok", file=None, close_modal=False, refresh=True, drawer=True, open_drawer=None, modal=None):
    """Finish a command.

    - toast: message shown bottom right
    - file: a GeneratedFile to show in the file dialog (acknowledgement, ASN, invoice, claim)
    - modal: (template, context) to show in the dialog area instead
    - open_drawer: URL of a record to open after the command
    """
    triggers = {}
    if toast:
        triggers["toast"] = {"msg": toast, "tone": tone}
    if refresh:
        triggers["refresh"] = True
    if drawer:
        triggers["drawerReload"] = True
    if close_modal:
        triggers["closeModal"] = True
    if open_drawer:
        triggers["openDrawer"] = {"url": open_drawer}
    if file is not None:
        resp = render(request, "dialogs/file.html", {"f": file, "rows": _rows(file.content)})
        resp["HX-Retarget"], resp["HX-Reswap"] = "#modal", "innerHTML"
    elif modal is not None:
        resp = render(request, modal[0], modal[1])
        resp["HX-Retarget"], resp["HX-Reswap"] = "#modal", "innerHTML"
    else:
        resp = HttpResponse(status=204)
    resp["HX-Trigger"] = json.dumps(triggers)
    return resp


def _rows(content):
    import csv
    import io
    return list(csv.reader(io.StringIO(content)))


def is_htmx(request):
    return bool(request.headers.get("HX-Request"))
