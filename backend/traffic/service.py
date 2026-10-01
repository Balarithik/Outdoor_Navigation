"""
Traffic service - main entry point for traffic management.
Coordinates presence, scoring, weights, and rerouting.
"""
import json
import logging
import time
from typing import Optional

from django.conf import settings
from channels.layers import get_channel_layer
from traffic.presence import PresenceManager
from traffic.scoring import TrafficScorer
from traffic.weights import WeightCalculator
from traffic.analyzer import RouteImpactAnalyzer
from routing_engine.manager import get_graph_manager
from cache.redis import get_redis_client
from analytics.services import AnalyticsService

logger = logging.getLogger('traffic')


class TrafficService:
    """Main service for traffic management."""

    def __init__(self):
        self.presence = PresenceManager()
        self.scorer = TrafficScorer()
        self.weights = WeightCalculator()
        self.analyzer = RouteImpactAnalyzer()
        self.graph_manager = get_graph_manager()
        self.analytics = AnalyticsService()
        self._traffic_version = 0
        self._simulation_prefix = "sim_"  # Prefix for simulation session IDs

    def _increment_traffic_version(self) -> int:
        """Increment the traffic version and persist it to Redis when available."""
        try:
            client = get_redis_client()
            if client:
                current = int(client.get('traffic_version') or 0)
                self._traffic_version = current + 1
                client.set('traffic_version', self._traffic_version)
                return self._traffic_version
        except Exception:
            pass

        self._traffic_version += 1
        return self._traffic_version

    def get_traffic_version(self) -> int:
        """Get current traffic version."""
        try:
            client = get_redis_client()
            if client:
                version = client.get('traffic_version')
                if version is not None:
                    self._traffic_version = int(version)
                    return self._traffic_version
        except Exception:
            pass
        return self._traffic_version

    def _get_channel_layer(self):
        """Get the Django Channels layer."""
        try:
            return get_channel_layer()
        except Exception:
            return None

    def _broadcast_to_group(self, group_name: str, event: dict):
        """Send an event to a channel group."""
        channel_layer = self._get_channel_layer()
        if channel_layer:
            try:
                async_to_sync = getattr(channel_layer, 'group_send', None)
                if async_to_sync:
                    # Try synchronous send
                    channel_layer.group_send(group_name, event)
                else:
                    # Async - create a task
                    import asyncio
                    asyncio.create_task(channel_layer.group_send(group_name, event))
            except Exception:
                pass

    def broadcast_traffic_update(self, navigation_id: str, edge_id: str, active_user_count: int, traffic_level: str, current_time_sec: float, traffic_version: int):
        """Broadcast traffic update to a navigation group."""
        event = {
            'type': 'traffic_update',
            'edge_id': edge_id,
            'active_user_count': active_user_count,
            'traffic_level': traffic_level,
            'current_time_sec': current_time_sec,
            'traffic_version': traffic_version,
        }
        self._broadcast_to_group(f'navigation_{navigation_id}', event)

    def broadcast_route_changed(self, navigation_id: str, route_id: str, reason: str, eta_sec: float, path: list, traffic_version: int, map_version: int):
        """Broadcast route change to a navigation group."""
        event = {
            'type': 'route_changed',
            'route_id': route_id,
            'reason': reason,
            'eta_sec': eta_sec,
            'path': path,
            'traffic_version': traffic_version,
            'map_version': map_version,
        }
        self._broadcast_to_group(f'navigation_{navigation_id}', event)

    def process_telemetry(self, session_id: str, lat: float, lng: float, timestamp: str = None, navigation_id: str = None) -> dict:
        """
        Process a telemetry update from a user.
        This is the main entry point for GPS data.

        Args:
            session_id: User's GPS sharing session ID
            lat: Latitude
            lng: Longitude
            timestamp: Optional timestamp
            navigation_id: Optional navigation session ID for routing updates
        """
        start_time = time.perf_counter()

        # Map match to edge
        graph = self.graph_manager.get_graph()
        if not graph or not graph.spatial_index:
            return {'success': False, 'error': 'Graph not available'}

        from routing_engine.matcher import MapMatcher
        matcher = MapMatcher(graph.spatial_index)
        match = matcher.match(lat, lng)

        if not match:
            return {'success': False, 'error': 'No matching road found'}

        edge_id = match['edge_id']

        # Update presence
        presence_result = self.presence.update_presence(session_id, edge_id, lat, lng)
        if not presence_result['success']:
            return presence_result

        active_count = presence_result['active_users']

        edge = graph.edges.get(edge_id)
        old_level = edge.get('traffic_level', 'LOW') if edge else 'LOW'
        new_level = self.scorer.classify_by_count(active_count)

        # Update edge weight
        if edge:
            old_time = edge.get('current_time_sec', edge.get('base_time_sec', 0))
            new_time = self.weights.update_edge_weight(edge, active_count)

            # Update in-memory graph
            self.graph_manager.update_edge_weight(edge_id, new_time)

            # Check if traffic level changed meaningfully
            if old_level != new_level:
                self._increment_traffic_version()
                self.analytics.record_traffic_state_change()
                logger.info(f"Traffic level changed on {edge_id}: {old_level} -> {new_level}")

                # Broadcast traffic update to navigation group if navigation_id provided
                if navigation_id:
                    self.broadcast_traffic_update(
                        navigation_id=navigation_id,
                        edge_id=edge_id,
                        active_user_count=active_count,
                        traffic_level=new_level,
                        current_time_sec=new_time if edge else 0,
                        traffic_version=self.get_traffic_version(),
                    )

        # Record telemetry
        self.analytics.record_telemetry()

        processing_time = (time.perf_counter() - start_time) * 1000

        return {
            'success': True,
            'edge_id': edge_id,
            'active_users': active_count,
            'traffic_level': new_level if edge else 'LOW',
            'match_confidence': match.get('confidence', 'LOW'),
            'match_distance_m': match.get('distance_m', 0),
            'processing_time_ms': round(processing_time, 2),
            'traffic_version': self.get_traffic_version(),
        }

    def get_traffic_state(self, edge_id: str) -> dict:
        """Get traffic state for a specific edge."""
        graph = self.graph_manager.get_graph()
        if not graph:
            return {'success': False, 'error': 'Graph not available'}

        edge = graph.edges.get(edge_id)
        if not edge:
            return {'success': False, 'error': 'Edge not found'}

        active_count = self.presence.get_edge_count(edge_id)
        state = self.weights.get_edge_state(edge, active_count)

        return {
            'success': True,
            'data': state,
        }

    def get_all_traffic(self) -> dict:
        """Get traffic state for all edges."""
        graph = self.graph_manager.get_graph()
        if not graph:
            return {'success': False, 'error': 'Graph not available'}

        active_edges = self.presence.get_all_active_edges()
        result = {}

        for edge_id, count in active_edges.items():
            edge = graph.edges.get(edge_id)
            if edge:
                state = self.weights.get_edge_state(edge, count)
                # Add geometry from edge's from/to node coordinates
                from_node = graph.nodes.get(edge.get('from'))
                to_node = graph.nodes.get(edge.get('to'))
                state['edge_id'] = edge_id
                state['name'] = edge.get('name', edge.get('road_name', edge_id))
                if from_node and to_node:
                    state['geometry'] = {
                        'type': 'LineString',
                        'coordinates': [
                            [from_node['lng'], from_node['lat']],
                            [to_node['lng'], to_node['lat']],
                        ],
                    }
                else:
                    state['geometry'] = {
                        'type': 'LineString',
                        'coordinates': [],
                    }
                result[edge_id] = state

        return {
            'success': True,
            'data': {
                'edges': result,
                'traffic_version': self.get_traffic_version(),
                'active_edge_count': len(result),
            }
        }

    def stop_sharing(self, session_id: str) -> dict:
        """Stop sharing location for a session."""
        result = self.presence.remove_presence(session_id)
        if result['success']:
            self._increment_traffic_version()
        return result

    def add_simulated_users(self, edge_id: str, count: int) -> dict:
        """Add simulated users to an edge (for demo mode)."""
        graph = self.graph_manager.get_graph()
        if not graph or edge_id not in graph.edges:
            return {'success': False, 'error': 'Edge not found'}

        # Add simulated presence using separate sim_* session IDs
        created_sessions = []
        for i in range(count):
            sim_session = f"{self._simulation_prefix}{edge_id}_{int(time.time())}_{i}"
            self.presence.update_presence(sim_session, edge_id, 0, 0)
            created_sessions.append(sim_session)

        # Track simulation session in the edge's sim users set
        try:
            client = get_redis_client()
            if client:
                sim_key = f"sim_users:{edge_id}"
                for sim_session in created_sessions:
                    client.sadd(sim_key, sim_session)
                client.expire(sim_key, self.presence.ttl * 2)
        except Exception:
            pass

        # Update weight - only count real sessions (session_*) for active_users
        # Simulated users should not contribute to the active count
        active_count = self.presence.get_edge_count(edge_id)

        edge = graph.edges[edge_id]
        self.weights.update_edge_weight(edge, active_count)
        self.graph_manager.update_edge_weight(edge_id, edge['current_time_sec'])
        # Do NOT increment traffic version just for adding simulated users
        # unless it changes the traffic level

        return {
            'success': True,
            'edge_id': edge_id,
            'active_users': active_count,  # Only real users counted
            'simulated': True,
        }

    def remove_simulated_users(self, edge_id: str, count: int = None) -> dict:
        """Remove simulated users from an edge."""
        graph = self.graph_manager.get_graph()
        if not graph or edge_id not in graph.edges:
            return {'success': False, 'error': 'Edge not found'}

        # Remove simulated users from the sim users set and session metadata without affecting real users.
        try:
            client = get_redis_client()
            if client:
                sim_key = f"sim_users:{edge_id}"
                sim_members = list(client.smembers(sim_key))
                if count is None:
                    for member in sim_members:
                        client.delete(self.presence._session_key(member))
                        client.srem(sim_key, member)
                    client.delete(sim_key)
                else:
                    for i, member in enumerate(sim_members):
                        if i >= count:
                            break
                        client.delete(self.presence._session_key(member))
                        client.srem(sim_key, member)
                    remaining = client.scard(sim_key)
                    if remaining > 0:
                        client.expire(sim_key, self.presence.ttl * 2)
                    else:
                        client.delete(sim_key)
        except Exception:
            pass

        # Update weight - only count real sessions for active_users
        active_count = self.presence.get_edge_count(edge_id)

        edge = graph.edges[edge_id]
        self.weights.update_edge_weight(edge, active_count)
        self.graph_manager.update_edge_weight(edge_id, edge['current_time_sec'])

        return {'success': True, 'edge_id': edge_id, 'simulated': True}

    def reset_simulation(self) -> dict:
        """Reset all simulated traffic. Does NOT remove real users."""
        graph = self.graph_manager.get_graph()
        if graph:
            # Only reset edge weights and traffic state
            for edge_id, edge in graph.edges.items():
                edge['current_time_sec'] = edge.get('base_time_sec', 0)
                edge['traffic_level'] = 'LOW'
                edge['traffic_factor'] = 1.0

        # Only clean up simulation users, not real user sessions
        try:
            client = get_redis_client()
            if client:
                sim_keys = client.keys("sim_users:*")
                for key in sim_keys:
                    client.delete(key)
        except Exception:
            pass

        # Do not increment traffic version on reset unless edge states actually changed
        # (traffic version should only change on meaningful traffic level changes)

        return {'success': True, 'simulated': True}
