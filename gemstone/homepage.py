from bdh_sdk import INFO, ON, WARN, Status

from . import cloud, service


def status():
    if not cloud.configured():
        return Status("Not set up", level=WARN)
    try:
        ctrls = service.controllers()
        states = [(c, service.public_state(c["id"])) for c in ctrls]
    except (cloud.CloudError, cloud.NotConfigured):
        return Status("Can't reach Gemstone", level=WARN, refresh=120)
    on = [(c, s) for c, s in states if s["on"]]
    if not on:
        return Status("Off", level=INFO, refresh=120)
    names = " + ".join(c["name"] for c, _ in on)
    pattern = next((s["pattern"]["name"] for _, s in on if s["pattern"]), "")
    text = f"{pattern} on {names}" if pattern else f"On · {names}"
    return Status(text, level=ON if pattern else INFO, refresh=120)
