"""
API views for map data management.
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticatedOrReadOnly, IsAdminUser
from mapdata.models import CampusFeature, MapVersion
from mapdata.serializers import CampusFeatureSerializer, MapVersionSerializer
from routing_engine.manager import get_graph_manager
from analytics.services import AnalyticsService

logger = logging.getLogger('mapdata')


def _is_valid_coord(value):
    if value is None:
        return False
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    if num != num or num in (float('inf'), float('-inf')):
        return False
    return True


def _validate_geometry(geometry_data, feature_type):
    if not isinstance(geometry_data, dict):
        raise ValueError('Geometry must be a GeoJSON object.')
    geom_type = geometry_data.get('type')
    coords = geometry_data.get('coordinates')
    if geom_type not in {'Point', 'LineString', 'Polygon'}:
        raise ValueError('Unsupported geometry type.')
    if not isinstance(coords, list) or not coords:
        raise ValueError('Geometry is empty or invalid.')

    if geom_type == 'Point':
        if len(coords) != 2 or not _is_valid_coord(coords[0]) or not _is_valid_coord(coords[1]):
            raise ValueError('Point geometry must contain valid latitude/longitude coordinates.')
        return

    if geom_type == 'LineString':
        if len(coords) < 2:
            raise ValueError('Road/path geometry must contain at least two coordinates.')
        for pair in coords:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError('LineString coordinates must be [lng, lat] pairs.')
            if not _is_valid_coord(pair[0]) or not _is_valid_coord(pair[1]):
                raise ValueError('LineString contains invalid coordinates.')
        return

    if geom_type == 'Polygon':
        if not isinstance(coords, list) or len(coords) < 1:
            raise ValueError('Polygon geometry is required.')
        for ring in coords:
            if not isinstance(ring, list) or len(ring) < 4:
                raise ValueError('Polygon rings must contain at least four coordinates.')
            for pair in ring:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError('Polygon coordinates must be [lng, lat] pairs.')
                if not _is_valid_coord(pair[0]) or not _is_valid_coord(pair[1]):
                    raise ValueError('Polygon contains invalid coordinates.')
        return


def _apply_route_invalidation(reason: str = 'map update'):
    try:
        MapVersion.increment(reason=reason)
        gm = get_graph_manager()
        gm.mark_stale()
        gm.reload_if_stale()
    except Exception:
        pass

    try:
        from cache.redis import get_redis_client
        client = get_redis_client()
        if client:
            keys = client.keys('route:*')
            for key in keys:
                client.delete(key)
    except Exception:
        pass


class MapDataView(APIView):
    """GET /api/v1/map - Get campus map data (OSM + custom features)."""

    def get(self, request):
        from django.conf import settings

        # Load OSM GeoJSON
        osm_data = None
        try:
            with open(settings.CAMPUS_GEOJSON_PATH) as f:
                osm_data = json.load(f)
        except FileNotFoundError:
            logger.warning(f"Campus GeoJSON not found: {settings.CAMPUS_GEOJSON_PATH}")

        # Load custom features
        custom_features = CampusFeature.objects.filter(is_active=True)
        custom_geojson = {
            'type': 'FeatureCollection',
            'features': [f.to_geojson() for f in custom_features],
        }

        # Get current map version
        map_version = MapVersion.get_current_version()

        return Response({
            'success': True,
            'data': {
                'osm': osm_data,
                'custom': custom_geojson,
                'map_version': map_version,
            }
        })


class MapVersionView(APIView):
    """GET /api/v1/map/version - Get current map version."""

    def get(self, request):
        version = MapVersion.get_current_version()
        return Response({
            'success': True,
            'data': {
                'map_version': version,
            }
        })


class AdminMapEditorView(APIView):
    """GET /api/v1/admin/map-editor/ - verify admin access to map editor."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        if not request.user or not request.user.is_authenticated:
            return Response({
                'success': False,
                'error': {
                    'code': 'AUTH_REQUIRED',
                    'message': 'Authentication required for the map editor.',
                },
            }, status=status.HTTP_401_UNAUTHORIZED)

        if not (request.user.is_staff or request.user.is_superuser):
            return Response({
                'success': False,
                'error': {
                    'code': 'ADMIN_REQUIRED',
                    'message': 'Admin access required to edit the campus map.',
                },
            }, status=status.HTTP_403_FORBIDDEN)

        return Response({
            'success': True,
            'data': {
                'allowed': True,
                'user': {
                    'username': request.user.username,
                    'is_staff': bool(request.user.is_staff),
                    'is_superuser': bool(request.user.is_superuser),
                },
                'map_version': MapVersion.get_current_version(),
            },
        })


class AdminMapEditorChangesView(APIView):
    """POST /api/v1/admin/map-editor/changes - batch admin map edit operations."""

    permission_classes = [IsAdminUser]

    def post(self, request):
        base_map_version = request.data.get('base_map_version')
        changes = request.data.get('changes') or []

        if base_map_version is None:
            return Response({
                'success': False,
                'error': {
                    'code': 'INVALID_REQUEST',
                    'message': 'base_map_version is required.',
                },
            }, status=status.HTTP_400_BAD_REQUEST)

        current_version = MapVersion.get_current_version()
        if int(base_map_version) != int(current_version):
            return Response({
                'success': False,
                'error': {
                    'code': 'MAP_VERSION_CONFLICT',
                    'message': 'The map has changed. Reload the latest version before saving.',
                },
            }, status=status.HTTP_409_CONFLICT)

        if not isinstance(changes, list):
            return Response({
                'success': False,
                'error': {
                    'code': 'INVALID_REQUEST',
                    'message': 'changes must be a list.',
                },
            }, status=status.HTTP_400_BAD_REQUEST)

        changed_ids = []
        routing_changed = False
        try:
            with transaction.atomic():
                for change in changes:
                    op = change.get('operation')
                    if op == 'create':
                        payload = change.get('feature') or {}
                        feature_type = payload.get('feature_type')
                        geometry_data = payload.get('geometry_data')
                        if not feature_type:
                            raise ValueError('Feature type is required.')
                        _validate_geometry(geometry_data, feature_type)
                        feature = CampusFeature.objects.create(
                            feature_type=feature_type,
                            name=payload.get('name', ''),
                            geometry_data=geometry_data,
                            properties=payload.get('properties', {}),
                            source=payload.get('source', CampusFeature.Source.ADMIN),
                            created_by=request.user.username if request.user.is_authenticated else 'admin',
                        )
                        changed_ids.append(str(feature.id))
                        if feature_type in ['road', 'path', 'pedestrian_area']:
                            routing_changed = True
                    elif op == 'update':
                        feature_id = change.get('feature_id')
                        payload = change.get('changes') or {}
                        feature = CampusFeature.objects.get(id=feature_id)
                        if 'name' in payload:
                            feature.name = payload['name']
                        if 'feature_type' in payload:
                            feature.feature_type = payload['feature_type']
                        if 'geometry_data' in payload:
                            _validate_geometry(payload['geometry_data'], feature.feature_type)
                            feature.geometry_data = payload['geometry_data']
                        if 'properties' in payload:
                            feature.properties = payload['properties']
                        if 'is_active' in payload:
                            feature.is_active = bool(payload['is_active'])
                        if 'source' in payload:
                            feature.source = payload['source']
                        feature.version += 1
                        feature.save()
                        changed_ids.append(str(feature.id))
                        if feature.feature_type in ['road', 'path', 'pedestrian_area']:
                            routing_changed = True
                    elif op == 'delete':
                        feature_id = change.get('feature_id')
                        feature = CampusFeature.objects.get(id=feature_id)
                        feature.is_active = False
                        feature.version += 1
                        feature.save()
                        changed_ids.append(str(feature.id))
                        if feature.feature_type in ['road', 'path', 'pedestrian_area']:
                            routing_changed = True
                    else:
                        raise ValueError(f'Unsupported operation: {op}')

                if routing_changed:
                    next_version = MapVersion.increment(reason='Admin map edit')
                    gm = get_graph_manager()
                    gm.mark_stale()
                    gm.reload_if_stale()
                    try:
                        from cache.redis import get_redis_client
                        client = get_redis_client()
                        if client:
                            for key in client.keys('route:*'):
                                client.delete(key)
                    except Exception:
                        pass
                else:
                    next_version = MapVersion.get_current_version()

                AnalyticsService().record_map_edit()
                return Response({
                    'success': True,
                    'data': {
                        'map_version': next_version.version if hasattr(next_version, 'version') else current_version,
                        'changed_feature_ids': changed_ids,
                    },
                }, status=status.HTTP_200_OK)
        except CampusFeature.DoesNotExist:
            return Response({
                'success': False,
                'error': {'code': 'FEATURE_NOT_FOUND', 'message': 'One or more requested features were not found.'},
            }, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({
                'success': False,
                'error': {'code': 'INVALID_GEOMETRY', 'message': str(exc)},
            }, status=status.HTTP_400_BAD_REQUEST)


class FeatureListCreateView(APIView):
    """GET/POST /api/v1/map/features - List or create features."""

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsAdminUser()]
        return [IsAuthenticatedOrReadOnly()]

    def get(self, request):
        features = CampusFeature.objects.filter(is_active=True)
        serializer = CampusFeatureSerializer(features, many=True)
        return Response({
            'success': True,
            'data': serializer.data,
        })

    def post(self, request):
        data = request.data

        # Validate required fields
        feature_type = data.get('feature_type')
        geometry_data = data.get('geometry_data')

        if not feature_type:
            return Response({
                'success': False,
                'error': {
                    'code': 'INVALID_FEATURE',
                    'message': 'feature_type is required.',
                }
            }, status=status.HTTP_400_BAD_REQUEST)

        if not geometry_data:
            return Response({
                'success': False,
                'error': {
                    'code': 'INVALID_FEATURE',
                    'message': 'geometry_data is required.',
                }
            }, status=status.HTTP_400_BAD_REQUEST)

        # Validate geometry type
        valid_types = ['Point', 'LineString', 'Polygon']
        geom_type = geometry_data.get('type')
        if geom_type not in valid_types:
            return Response({
                'success': False,
                'error': {
                    'code': 'INVALID_GEOMETRY',
                    'message': f'Geometry type must be one of: {", ".join(valid_types)}.',
                }
            }, status=status.HTTP_400_BAD_REQUEST)

        # Create feature
        feature = CampusFeature.objects.create(
            feature_type=feature_type,
            name=data.get('name', ''),
            geometry_data=geometry_data,
            properties=data.get('properties', {}),
            source=data.get('source', 'admin'),
            created_by=request.user.username if request.user.is_authenticated else 'anonymous',
        )

        routing_relevant = feature_type in ['road', 'path', 'pedestrian_area']
        if routing_relevant:
            MapVersion.increment(reason=f"Added {feature_type}: {feature.name or 'unnamed'}")
            gm = get_graph_manager()
            gm.mark_stale()

            # Invalidate route cache
            try:
                from cache.redis import get_redis_client
                client = get_redis_client()
                if client:
                    keys = client.keys("route:*")
                    for key in keys:
                        client.delete(key)
            except Exception:
                pass

        AnalyticsService().record_map_edit()

        serializer = CampusFeatureSerializer(feature)
        return Response({
            'success': True,
            'data': serializer.data,
        }, status=status.HTTP_201_CREATED)


class FeatureDetailView(APIView):
    """GET/PUT/DELETE /api/v1/map/features/{id} - Feature detail operations."""

    def get_permissions(self):
        if self.request.method in ['PUT', 'DELETE']:
            return [IsAdminUser()]
        return [IsAuthenticatedOrReadOnly()]

    def get_object(self, feature_id):
        try:
            return CampusFeature.objects.get(id=feature_id)
        except CampusFeature.DoesNotExist:
            return None

    def get(self, request, feature_id):
        feature = self.get_object(feature_id)
        if not feature:
            return Response({
                'success': False,
                'error': {
                    'code': 'FEATURE_NOT_FOUND',
                    'message': 'Feature not found.',
                }
            }, status=status.HTTP_404_NOT_FOUND)

        serializer = CampusFeatureSerializer(feature)
        return Response({
            'success': True,
            'data': serializer.data,
        })

    def put(self, request, feature_id):
        feature = self.get_object(feature_id)
        if not feature:
            return Response({
                'success': False,
                'error': {
                    'code': 'FEATURE_NOT_FOUND',
                    'message': 'Feature not found.',
                }
            }, status=status.HTTP_404_NOT_FOUND)

        data = request.data

        # Update fields
        if 'name' in data:
            feature.name = data['name']
        if 'geometry_data' in data:
            feature.geometry_data = data['geometry_data']
        if 'properties' in data:
            feature.properties = data['properties']
        if 'is_active' in data:
            feature.is_active = data['is_active']

        feature.version += 1
        feature.save()

        # Increment map version only for routing-relevant edits
        if feature.feature_type in ['road', 'path', 'pedestrian_area']:
            MapVersion.increment(reason=f"Updated {feature.feature_type}: {feature.name or 'unnamed'}")
            gm = get_graph_manager()
            gm.mark_stale()

            # Invalidate route cache
            try:
                from cache.redis import get_redis_client
                client = get_redis_client()
                if client:
                    keys = client.keys("route:*")
                    for key in keys:
                        client.delete(key)
            except Exception:
                pass

        AnalyticsService().record_map_edit()

        serializer = CampusFeatureSerializer(feature)
        return Response({
            'success': True,
            'data': serializer.data,
        })

    def delete(self, request, feature_id):
        feature = self.get_object(feature_id)
        if not feature:
            return Response({
                'success': False,
                'error': {
                    'code': 'FEATURE_NOT_FOUND',
                    'message': 'Feature not found.',
                }
            }, status=status.HTTP_404_NOT_FOUND)

        # Soft delete
        feature.is_active = False
        feature.save()

        # Increment map version only for routing-relevant deletions
        if feature.feature_type in ['road', 'path', 'pedestrian_area']:
            MapVersion.increment(reason=f"Deleted {feature.feature_type}: {feature.name or 'unnamed'}")
            gm = get_graph_manager()
            gm.mark_stale()

            # Invalidate route cache
            try:
                from cache.redis import get_redis_client
                client = get_redis_client()
                if client:
                    keys = client.keys("route:*")
                    for key in keys:
                        client.delete(key)
            except Exception:
                pass

        AnalyticsService().record_map_edit()

        return Response({
            'success': True,
            'data': {'id': str(feature.id), 'deleted': True},
        })
