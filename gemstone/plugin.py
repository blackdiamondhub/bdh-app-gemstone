from bdh_sdk import PluginSpec, Setting, SettingsPage, Tile
from bdh_sdk.plugins import LIFECYCLE_ALWAYS, NETWORK_OUTBOUND, SECRET, STR

spec = PluginSpec(
    id="gemstone",
    app="gemstone",
    urls="gemstone.urls",
    tiles=(
        Tile("Gemstone", "gemstone_index", "gemstone/hub/icon.svg", section="lodge",
             subtitle="Gemstone patterns, colours and designs", order=40),
    ),
    status="gemstone.homepage.status",
    settings=(
        Setting("gemstone_email", "Gemstone email", type=STR, default="",
                help="The email you sign in to the Gemstone Lights app with."),
        Setting("gemstone_password", "Gemstone password", type=SECRET),
    ),
    settings_pages=(
        SettingsPage("Photos & light paths", "gemstone_settings", icon="fas fa-lightbulb",
                     subtitle="A photo of each controller's lights and where they run"),
    ),
    api_version=2,
    # The Gemstone cloud.
    network=NETWORK_OUTBOUND,
    # "Keep on until" turns the lights off at a set minute, "lights off tonight"
    # keeps them off till morning, and guest designs are cleaned up daily, so
    # the worker has to keep running.
    lifecycle=LIFECYCLE_ALWAYS,
    # Measured: ~52 MB idle, pycognito/boto3 add ~55 MB once signed in to
    # Gemstone, and stylizing an uploaded photo peaks ~40 MB more.
    memory_mb=160,
)
