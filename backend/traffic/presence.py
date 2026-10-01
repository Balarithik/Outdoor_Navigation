"""
Active user presence tracking using Redis.
Each session has a TTL-based presence on edges.
"""
import json
import logging
import time
from typing import Optional, List

from django.conf import settings
from cache.redis import get_redis_client

logger = logging.getLogger('traffic')


class PresenceManager:
    """Manages active user presence on routing edges using Redis."""

    def __init__(self):
        self.ttl = settings.TRAFFIC_LOCATION_TTL_SECONDS

    def _presence_key(self, edge_id: str) -> str:
        return f"active_users:{edge_id}"

    def _session_key(self, session_id: str) -> str:
        return f"session:{session_id}"

    def update_presence(self, session_id: str, edge_id: str, lat: float, lng: float) -> dict:
        """
        Update user presence on an edge.
        Removes previous edge membership and adds new one.
        Real sessions contribute to active counts; simulation sessions are tracked separately.
        """
        client = get_redis_client()
        if not client:
            return {'success': False, 'error': 'Redis unavailable'}

        try:
            session_key = self._session_key(session_id)
            edge_set_key = self._presence_key(edge_id)
            if session_id.startswith('sim_'):
                edge_set_key = f"sim_users:{edge_id}"

            # Get previous edge for this session
            session_data = client.get(session_key)
            if session_data:
                prev = json.loads(session_data)
                prev_edge = prev.get('edge_id')
                if prev_edge and prev_edge != edge_id:
                    prev_set_key = self._presence_key(prev_edge)
                    if session_id.startswith('sim_'):
                        prev_set_key = f"sim_users:{prev_edge}"
                    client.srem(prev_set_key, session_id)
                    logger.info(f"Session {session_id} removed from edge {prev_edge}")

            # Add to new edge
            client.sadd(edge_set_key, session_id)

            # Update session data with TTL
            session_info = {
                'edge_id': edge_id,
                'lat': lat,
                'lng': lng,
                'last_seen': time.time(),
            }
            client.setex(session_key, self.ttl, json.dumps(session_info))

            # Refresh edge TTL (edge expires if no active users)
            client.expire(edge_set_key, self.ttl * 2)

            # Get updated count, cleaning up any expired sessions
            count = self.get_edge_count(edge_id)

            logger.info(f"Session {session_id} on edge {edge_id}, count={count}")

            return {
                'success': True,
                'edge_id': edge_id,
                'active_users': count,
            }

        except Exception as e:
            logger.error(f"Presence update failed: {e}")
            return {'success': False, 'error': str(e)}

    def remove_presence(self, session_id: str) -> dict:
        """Remove a user's presence from all edges."""
        client = get_redis_client()
        if not client:
            return {'success': False, 'error': 'Redis unavailable'}

        try:
            session_data = client.get(self._session_key(session_id))
            if session_data:
                prev = json.loads(session_data)
                prev_edge = prev.get('edge_id')
                if prev_edge:
                    set_key = self._presence_key(prev_edge)
                    if session_id.startswith('sim_'):
                        set_key = f"sim_users:{prev_edge}"
                    client.srem(set_key, session_id)
                    count = client.scard(set_key)
                    logger.info(f"Session {session_id} removed from edge {prev_edge}, count={count}")

            client.delete(self._session_key(session_id))

            return {'success': True}
        except Exception as e:
            logger.error(f"Presence removal failed: {e}")
            return {'success': False, 'error': str(e)}

    def _is_session_valid(self, session_data: str) -> bool:
        """Check if session data is still valid (not expired)."""
        if not session_data:
            return False
        try:
            parsed = json.loads(session_data)
            last_seen = parsed.get('last_seen', 0)
            # Session is valid if last_seen is within TTL
            return (time.time() - last_seen) < self.ttl
        except (json.JSONDecodeError, TypeError):
            return False

    def get_edge_count(self, edge_id: str) -> int:
        """Get active user count for an edge, filtering out expired sessions."""
        client = get_redis_client()
        if not client:
            return 0
        try:
            # Real sessions contribute to active counts. Simulation sessions are tracked separately.
            members = client.smembers(self._presence_key(edge_id))
            valid_members = 0

            for member in members:
                if member.startswith('sim_'):
                    continue
                member_data = client.get(self._session_key(member))
                if member_data and self._is_session_valid(member_data):
                    valid_members += 1
                else:
                    client.srem(self._presence_key(edge_id), member)

            return valid_members
        except Exception:
            return 0

    def get_all_active_edges(self) -> dict:
        """Get all edges with active users, filtering out expired sessions."""
        client = get_redis_client()
        if not client:
            return {}
        try:
            keys = client.keys("active_users:*")
            result = {}
            for key in keys:
                edge_id = key.decode().split(":", 1)[1] if isinstance(key, bytes) else key.split(":", 1)[1]
                count = self.get_edge_count(edge_id)
                if count > 0:
                    result[edge_id] = count
            return result
        except Exception:
            return {}

    def get_session_edge(self, session_id: str) -> Optional[str]:
        """Get the edge a session is currently on."""
        client = get_redis_client()
        if not client:
            return None
        try:
            data = client.get(self._session_key(session_id))
            if data:
                # Check if session is still valid
                if self._is_session_valid(data):
                    return json.loads(data).get('edge_id')
            return None
        except Exception:
            pass
        return None

    def cleanup_expired(self) -> int:
        """Clean up expired sessions. Returns count of removed sessions."""
        # Redis TTL handles expiration automatically
        # This method can be used for additional cleanup if needed
        return 0
