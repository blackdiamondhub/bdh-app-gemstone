import json
import logging

from bdh_sdk import is_admin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import cloud, layout, service, worker

log = logging.getLogger(__name__)
MAX_UPLOAD = 9 * 1024 * 1024


def _body(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


def _error(msg, status=400):
    return JsonResponse({"error": str(msg)}, status=status)


def _guarded(fn):
    """Turn the service's errors into JSON responses."""
    def wrapper(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except cloud.NotConfigured:
            return _error("Gemstone isn't set up yet — an admin needs to enter the account in Settings", 409)
        except (service.Forbidden, PermissionError) as exc:
            return _error(exc, 403)
        except (service.Invalid, layout.Invalid) as exc:
            return _error(exc, 400)
        except cloud.CloudError as exc:
            log.warning("Gemstone cloud error: %s", exc)
            return _error("Gemstone didn't respond — try again in a moment", 502)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _admin_only(request):
    if not is_admin(request):
        raise service.Forbidden("Only a hub admin can do that")


def _controller_json(c):
    data = layout.get(c["id"])
    return dict(c, layout=data)


def _state(request):
    admin = is_admin(request)
    out = {"admin": admin, "configured": cloud.configured(), "guest_folder": service.GUEST_FOLDER,
           "guest_last": service.GUEST_LAST.strftime("%H:%M"), "keep_days": service.GUEST_KEEP_DAYS,
           "animations": [{"label": a, "name": n} for a, n in service.ANIMATIONS],
           "more_animations": list(service.MORE_ANIMATIONS), "controllers": [], "until": None}
    if not out["configured"]:
        return out
    try:
        ctrls = service.controllers()
        out["controllers"] = [dict(_controller_json(c), state=service.public_state(c["id"])) for c in ctrls]
        out["until"] = service.until()
        out["guest_deadline"] = service.guest_deadline().isoformat()
    except cloud.CloudError as exc:
        out["error"] = "Can't reach Gemstone right now"
        log.warning("state: %s", exc)
    return out


def index(request):
    return render(request, "gemstone/index.html", {"state": _state(request)})


@require_GET
def settings_page(request):
    # Settings → Gemstone → Photos & light paths. Admins only, like the rest of Settings.
    if not is_admin(request):
        raise Http404
    return render(request, "gemstone/settings.html", {"active": "gemstone", "state": _state(request)})


@require_GET
def state(request):
    return JsonResponse(_state(request))


@require_GET
@_guarded
def library(request):
    return JsonResponse({"patterns": service.library()})


@require_GET
@_guarded
def ours(request):
    data = service.ours()
    if not is_admin(request):
        # Guests see the house's folders, but only save into their own.
        data = dict(data, folders=[dict(f, locked=not f["guest"]) for f in data["folders"]])
    return JsonResponse(data)


@require_POST
@_guarded
def play(request):
    b = _body(request)
    return JsonResponse(service.play(b.get("ids"), b.get("pattern")))


@require_POST
@_guarded
def undo(request):
    return JsonResponse(service.undo(_body(request).get("token", "")))


@require_POST
@_guarded
def power(request):
    b = _body(request)
    out = service.power(b.get("ids"), bool(b.get("on")), admin=is_admin(request))
    worker.poke()
    return JsonResponse(out)


@require_POST
@_guarded
def brightness(request):
    b = _body(request)
    return JsonResponse(service.brightness(b.get("ids"), b.get("brightness")))


@require_POST
@_guarded
def until(request):
    b = _body(request)
    out = service.set_until(b.get("ids"), b.get("at"), admin=is_admin(request))
    worker.poke()
    return JsonResponse({"until": out})


@require_POST
@_guarded
def design(request):
    b = _body(request)
    admin = is_admin(request)
    out = service.save_design(b.get("pattern"), admin=admin, design_id=b.get("id", ""),
                              folder_id=b.get("folder", "") if admin else "",
                              name=b.get("name", "") if admin else "")
    return JsonResponse(out)


@require_POST
@_guarded
def live(request):
    b = _body(request)
    return JsonResponse(service.live(b.get("ids"), b.get("pattern")))


@require_POST
@_guarded
def folders(request):
    _admin_only(request)
    return JsonResponse(service.new_folder(_body(request).get("name", "")))


@require_http_methods(["GET", "POST"])
@_guarded
def layout_view(request, cid):
    layout.check_cid(cid)
    ctrl = next((c for c in service.controllers() if c["id"] == cid), None)
    if ctrl is None:
        raise Http404
    if request.method == "GET":
        return JsonResponse(layout.get(cid))
    _admin_only(request)
    b = _body(request)
    return JsonResponse(layout.save(cid, b.get("runs")))


@require_POST
@_guarded
def photo_upload(request, cid):
    _admin_only(request)
    layout.check_cid(cid)
    ctrl = next((c for c in service.controllers() if c["id"] == cid), None)
    if ctrl is None:
        raise Http404
    if request.POST.get("remove"):
        return JsonResponse(layout.remove_photo(cid))
    f = request.FILES.get("photo")
    if f is None:
        return _error("choose a photo")
    if f.size > MAX_UPLOAD:
        return _error("that photo is too big (9 MB max)")
    return JsonResponse(layout.upload_photo(cid, f.read()))


@require_GET
def photo(request, cid):
    try:
        data = layout.photo_bytes(cid)
    except layout.Invalid:
        raise Http404
    if data is None:
        raise Http404
    resp = HttpResponse(data, content_type="image/jpeg")
    resp["Cache-Control"] = "max-age=86400"
    return resp


