from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="gemstone_index"),
    path("settings/", views.settings_page, name="gemstone_settings"),
    path("api/state/", views.state, name="gemstone_state"),
    path("api/library/", views.library, name="gemstone_library"),
    path("api/ours/", views.ours, name="gemstone_ours"),
    path("api/play/", views.play, name="gemstone_play"),
    path("api/undo/", views.undo, name="gemstone_undo"),
    path("api/power/", views.power, name="gemstone_power"),
    path("api/brightness/", views.brightness, name="gemstone_brightness"),
    path("api/until/", views.until, name="gemstone_until"),
    path("api/schedule/", views.schedule, name="gemstone_schedule"),
    path("api/autopilot/", views.autopilot_api, name="gemstone_autopilot_api"),
    path("api/plan/", views.plan, name="gemstone_plan"),
    path("api/plan/schedules/", views.schedule_save, name="gemstone_schedule_save"),
    path("api/plan/schedules/toggle/", views.schedule_toggle, name="gemstone_schedule_toggle"),
    path("api/plan/schedules/delete/", views.schedule_delete, name="gemstone_schedule_delete"),
    path("api/activity/", views.activity, name="gemstone_activity"),
    path("api/design/", views.design, name="gemstone_design"),
    path("api/design/delete/", views.design_delete, name="gemstone_design_delete"),
    path("api/design/restore/", views.design_restore, name="gemstone_design_restore"),
    path("api/live/", views.live, name="gemstone_live"),
    path("api/fav/", views.fav, name="gemstone_fav"),
    path("api/folders/", views.folders, name="gemstone_folders"),
    path("api/layout/<str:cid>/", views.layout_view, name="gemstone_layout"),
    path("api/photo/<str:cid>/", views.photo_upload, name="gemstone_photo_upload"),
    path("photo/<str:cid>.jpg", views.photo, name="gemstone_photo"),
]
