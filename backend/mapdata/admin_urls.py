"""Routes for admin-only map editing operations."""
from django.urls import path

from mapdata.views import AdminMapEditorView, AdminMapEditorChangesView

urlpatterns = [
    path('map-editor/', AdminMapEditorView.as_view(), name='admin-map-editor'),
    path('map-editor/changes', AdminMapEditorChangesView.as_view(), name='admin-map-editor-changes'),
]
