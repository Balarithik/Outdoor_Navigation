import React, { useState, useEffect, useCallback } from 'react'
import CampusMap from './components/map/CampusMap'
import SearchBox from './components/navigation/SearchBox'
import RoutePanel from './components/navigation/RoutePanel'
import NavigationPanel from './components/navigation/NavigationPanel'
import TrafficPanel from './components/traffic/TrafficPanel'
import TradeoffPanel from './components/traffic/TradeoffPanel'
import TrafficDemo from './components/demo/TrafficDemo'
import MapEditor from './components/admin/MapEditor'
import { useCampusData } from './hooks/useCampusData'
import { useGeolocation } from './hooks/useGeolocation'
import { useNavigation } from './hooks/useNavigation'
import { useTraffic } from './hooks/useTraffic'
import { useWebSocket } from './hooks/useWebSocket'
import { computeRoute } from './services/routeApi'
import RerouteNotice from './components/navigation/RerouteNotice'

export default function App() {
  const [view, setView] = useState(() => {
    const path = window.location.pathname || '/'
    if (path.startsWith('/admin')) return 'admin'
    if (path.startsWith('/demo')) return 'demo'
    return 'map'
  })
  const [selectedPlace, setSelectedPlace] = useState(null)
  const [route, setRoute] = useState(null)
  const [showTradeoff, setShowTradeoff] = useState(false)
  const [rerouteData, setRerouteData] = useState(null)
  const { position, error: geoError, loading: geoLoading, getCurrentPosition } = useGeolocation()
  const { mapData, places, loading: mapLoading, error: mapError } = useCampusData()
  const { navigation, startNavigation, stopNavigation, updateLocation } = useNavigation()
  const { trafficData, trafficVersion, activeUsers } = useTraffic()
  const { connected, lastMessage } = useWebSocket(navigation?.navigation_id)

  // Continuous GPS updates for active navigation
  useEffect(() => {
    if (!navigation || !position) return
    const watchId = navigator.geolocation.watchPosition(
      (pos) => {
        updateLocation(
          pos.coords.latitude,
          pos.coords.longitude,
          null  // edge_id will be determined server-side via map matching
        )
      },
      (err) => {
        console.error('Navigation GPS watch error:', err.message)
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 5000 }
    )
    return () => {
      navigator.geolocation.clearWatch(watchId)
    }
  }, [navigation, position, updateLocation])

  useEffect(() => {
    const syncViewFromPath = () => {
      const path = window.location.pathname || '/'
      if (path.startsWith('/admin')) setView('admin')
      else if (path.startsWith('/demo')) setView('demo')
      else setView('map')
    }

    window.addEventListener('popstate', syncViewFromPath)
    syncViewFromPath()

    return () => window.removeEventListener('popstate', syncViewFromPath)
  }, [])

  const changeView = useCallback((nextView) => {
    setView(nextView)
    const pathname = nextView === 'admin' ? '/admin/map-editor' : nextView === 'demo' ? '/demo' : '/'
    if (window.location.pathname !== pathname) {
      window.history.pushState({}, '', pathname)
    }
  }, [])

  // Handle place selection
  const handlePlaceSelect = useCallback((place) => {
    setSelectedPlace(place)
    if (!position || !place) {
      return
    }

    computeRoute(
      { lat: position.lat, lng: position.lng },
      { place_id: place.id },
      'walking'
    ).then((result) => {
      if (result?.success) {
        setRoute(result.data)
      } else {
        console.error('Route request failed:', result?.error || 'Unknown route error')
      }
    }).catch((err) => {
      console.error('Route request failed:', err)
    })
  }, [position])

  // Handle route request using computeRoute from routeApi
  const computeRouteRequest = useCallback(async (source, destination) => {
    try {
      const result = await computeRoute(source, destination, 'walking')
      if (result.success) {
        setRoute(result.data)
      }
    } catch (err) {
      console.error('Route request failed:', err)
    }
  }, [])

  // Handle navigation start
  const handleStartNavigation = useCallback(async () => {
    if (!route) return
    await startNavigation(route)
  }, [route, startNavigation])

  // Handle navigation stop
  const handleStopNavigation = useCallback(async () => {
    await stopNavigation()
    setRoute(null)
  }, [stopNavigation])

  // Handle reroute from WebSocket
  useEffect(() => {
    if (lastMessage?.event === 'route_changed') {
      setRoute(prev => ({
        ...prev,
        path: lastMessage.path,
        duration_sec: lastMessage.eta_sec,
        traffic_version: lastMessage.traffic_version,
      }))
      setRerouteData({
        event: 'route_changed',
        route_id: lastMessage.route_id,
        reason: lastMessage.reason,
        eta_sec: lastMessage.eta_sec,
        path: lastMessage.path,
        traffic_version: lastMessage.traffic_version,
        map_version: lastMessage.map_version,
      })
    }
  }, [lastMessage])

  return (
    <div className="app">
      {/* Top Navigation Bar */}
      <header className="app-header">
        <div className="logo">
          <svg viewBox="0 0 100 100" width="32" height="32">
            <circle cx="50" cy="50" r="45" fill="#2563eb" />
            <path d="M30 65 L50 25 L70 65 Z" fill="white" />
            <circle cx="50" cy="55" r="8" fill="#2563eb" />
          </svg>
          <h1>CampusFlow</h1>
        </div>
        <nav className="nav-tabs">
          <button
            className={view === 'map' ? 'active' : ''}
            onClick={() => changeView('map')}
          >
            Map
          </button>
          <button
            className={view === 'demo' ? 'active' : ''}
            onClick={() => changeView('demo')}
          >
            Traffic Demo
          </button>
          <button
            className={view === 'admin' ? 'active' : ''}
            onClick={() => changeView('admin')}
          >
            Admin
          </button>
          <button
            className={showTradeoff ? 'active' : ''}
            onClick={() => setShowTradeoff(!showTradeoff)}
          >
            Metrics
          </button>
        </nav>
        <div className="header-status">
          <span className={`status-dot ${connected ? 'connected' : 'disconnected'}`} />
          <span>{connected ? 'Live' : 'Offline'}</span>
        </div>
      </header>

      {/* Main Content */}
      <main className="app-main">
        {view === 'map' && (
          <>
            <SearchBox
              places={places}
              onPlaceSelect={handlePlaceSelect}
              selectedPlace={selectedPlace}
            />
            <CampusMap
              mapData={mapData}
              places={places}
              route={route}
              trafficData={trafficData}
              selectedPlace={selectedPlace}
              onPlaceSelect={handlePlaceSelect}
              loading={mapLoading}
            />
            {route && (
              <RoutePanel
                route={route}
                onStartNavigation={handleStartNavigation}
                onClearRoute={() => setRoute(null)}
              />
            )}
            {navigation && (
              <NavigationPanel
                navigation={navigation}
                onStop={handleStopNavigation}
                route={route}
              />
            )}
            <TrafficPanel
              trafficData={trafficData}
              trafficVersion={trafficVersion}
              activeUsers={activeUsers}
            />
          </>
        )}

        {rerouteData && <RerouteNotice rerouteData={rerouteData} onDismiss={() => setRerouteData(null)} />}

        {view === 'demo' && (
          <TrafficDemo />
        )}

        {view === 'admin' && (
          <MapEditor mapData={mapData} />
        )}

        {showTradeoff && (
          <TradeoffPanel onClose={() => setShowTradeoff(false)} />
        )}
      </main>

      {/* Map Attribution */}
      <footer className="map-attribution">
        <span>© OpenStreetMap contributors</span>
        <span>•</span>
        <span>CampusFlow</span>
      </footer>
    </div>
  )
}
