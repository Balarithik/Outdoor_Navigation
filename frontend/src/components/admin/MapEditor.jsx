/**
 * Admin map editor component.
 */
import React, { useState, useEffect, useCallback, useRef } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { getFeatures, getAdminMapEditorAccess, saveMapEditorChanges } from '../../services/mapApi'

const FEATURE_TYPES = [
  { value: 'road', label: 'Road' },
  { value: 'path', label: 'Path' },
  { value: 'pedestrian_area', label: 'Pedestrian Area' },
  { value: 'building', label: 'Building' },
  { value: 'gate', label: 'Gate' },
  { value: 'parking', label: 'Parking' },
  { value: 'landmark', label: 'Landmark' },
  { value: 'facility', label: 'Facility' },
  { value: 'other', label: 'Other' },
]

const TOOL_OPTIONS = [
  { value: 'select', label: 'Select' },
  { value: 'point', label: 'Add Point' },
  { value: 'line', label: 'Draw Line' },
  { value: 'area', label: 'Draw Area' },
  { value: 'edit', label: 'Edit' },
  { value: 'delete', label: 'Delete' },
]

const MAP_STYLE = import.meta.env.VITE_MAP_STYLE_URL || 'https://demotiles.maplibre.org/style.json'
const CAMPUS_CENTER = [77.68, 9.57]
const DEFAULT_ZOOM = 15

function cloneValue(value) {
  return JSON.parse(JSON.stringify(value))
}

function featureToGeojson(feature) {
  if (!feature || !feature.geometry_data) return null
  return {
    type: 'Feature',
    id: feature.id,
    geometry: feature.geometry_data,
    properties: {
      id: feature.id,
      feature_type: feature.feature_type,
      name: feature.name || 'Unnamed',
      source: feature.source,
      is_active: feature.is_active,
      ...feature.properties,
    },
  }
}

function featuresToGeojson(features) {
  return {
    type: 'FeatureCollection',
    features: features
      .filter((feature) => feature && feature.geometry_data && feature.is_active !== false)
      .map(featureToGeojson)
      .filter(Boolean),
  }
}

export default function MapEditor({ mapData }) {
  const mapContainer = useRef(null)
  const mapRef = useRef(null)
  const [accessState, setAccessState] = useState({ loading: true, allowed: false, user: null })
  const [features, setFeatures] = useState([])
  const [selectedTool, setSelectedTool] = useState('select')
  const [selectedFeatureType, setSelectedFeatureType] = useState('road')
  const [selectedFeatureId, setSelectedFeatureId] = useState(null)
  const [pendingChanges, setPendingChanges] = useState([])
  const [draftPoints, setDraftPoints] = useState([])
  const [message, setMessage] = useState(null)
  const [loading, setLoading] = useState(false)
  const [baseMapVersion, setBaseMapVersion] = useState(mapData?.map_version ?? 0)
  const [historyState, setHistoryState] = useState({ items: [], index: -1 })

  const selectedFeature = features.find((feature) => feature.id === selectedFeatureId) || null
  const unsavedCount = pendingChanges.length

  const checkpoint = useCallback((nextFeatures) => {
    const snapshot = cloneValue(nextFeatures)
    setHistoryState((prev) => {
      const items = prev.items.slice(0, prev.index + 1)
      const updated = [...items, snapshot]
      return { items: updated, index: updated.length - 1 }
    })
  }, [])

  const loadFeatures = useCallback(async () => {
    try {
      const result = await getFeatures()
      if (result?.success) {
        const nextFeatures = result.data || []
        setFeatures(nextFeatures)
        setHistoryState({ items: [cloneValue(nextFeatures)], index: 0 })
        setSelectedFeatureId(null)
      }
    } catch (err) {
      console.error('Failed to load features:', err)
    }
  }, [])

  const checkAccess = useCallback(async () => {
    try {
      const result = await getAdminMapEditorAccess()
      setAccessState({
        loading: false,
        allowed: Boolean(result?.success && result?.data?.allowed),
        user: result?.data?.user || null,
        error: null,
      })
    } catch (err) {
      setAccessState({
        loading: false,
        allowed: false,
        user: null,
        error: err.message || 'Admin access required.',
      })
    }
  }, [])

  useEffect(() => {
    checkAccess()
    loadFeatures()
  }, [checkAccess, loadFeatures])

  useEffect(() => {
    if (!mapData?.map_version) return
    setBaseMapVersion(mapData.map_version)
  }, [mapData])

  useEffect(() => {
    if (!mapRef.current) return

    const nextData = featuresToGeojson(features)
    const customSource = mapRef.current.getSource('custom-features')
    if (customSource) {
      customSource.setData(nextData)
    }

    if (selectedFeatureId) {
      const selectedFeatureData = {
        type: 'FeatureCollection',
        features: features
          .filter((feature) => feature.id === selectedFeatureId)
          .map(featureToGeojson)
          .filter(Boolean),
      }
      const selectedSource = mapRef.current.getSource('selected-feature')
      if (selectedSource) {
        selectedSource.setData(selectedFeatureData)
      }
    } else {
      const selectedSource = mapRef.current.getSource('selected-feature')
      if (selectedSource) {
        selectedSource.setData({ type: 'FeatureCollection', features: [] })
      }
    }

    if (draftPoints.length > 0) {
      const draftGeojson = {
        type: 'FeatureCollection',
        features: [{
          type: 'Feature',
          geometry: {
            type: selectedTool === 'area' ? 'Polygon' : 'LineString',
            coordinates: selectedTool === 'area'
              ? [draftPoints.concat([draftPoints[0]])]
              : draftPoints,
          },
          properties: { draft: true },
        }],
      }
      const draftSource = mapRef.current.getSource('draft-feature')
      if (draftSource) {
        draftSource.setData(draftGeojson)
      }
    } else {
      const draftSource = mapRef.current.getSource('draft-feature')
      if (draftSource) {
        draftSource.setData({ type: 'FeatureCollection', features: [] })
      }
    }
  }, [features, selectedFeatureId, draftPoints, selectedTool])

  useEffect(() => {
    if (mapRef.current || !mapContainer.current) return

    const map = new maplibregl.Map({
      container: mapContainer.current,
      style: MAP_STYLE,
      center: CAMPUS_CENTER,
      zoom: DEFAULT_ZOOM,
      attributionControl: { compact: true },
    })

    map.addControl(new maplibregl.NavigationControl(), 'top-right')
    map.addControl(new maplibregl.ScaleControl(), 'bottom-left')

    map.on('load', () => {
      mapRef.current = map

      map.addSource('custom-features', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })

      map.addSource('selected-feature', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })

      map.addSource('draft-feature', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })

      map.addLayer({
        id: 'custom-roads',
        type: 'line',
        source: 'custom-features',
        filter: ['in', ['get', 'feature_type'], ['literal', ['road', 'path', 'pedestrian_area']]],
        paint: {
          'line-color': ['case', ['==', ['get', 'feature_type'], 'road'], '#2563eb', ['==', ['get', 'feature_type'], 'path'], '#22c55e', '#8b5cf6'],
          'line-width': 4,
        },
      })

      map.addLayer({
        id: 'custom-buildings',
        type: 'fill',
        source: 'custom-features',
        filter: ['in', ['get', 'feature_type'], ['literal', ['building', 'pedestrian_area', 'parking']]],
        paint: {
          'fill-color': '#dbeafe',
          'fill-opacity': 0.8,
        },
      })

      map.addLayer({
        id: 'custom-points',
        type: 'circle',
        source: 'custom-features',
        filter: ['==', ['geometry-type'], 'Point'],
        paint: {
          'circle-radius': 8,
          'circle-color': '#f59e0b',
          'circle-stroke-width': 2,
          'circle-stroke-color': '#fff7ed',
        },
      })

      map.addLayer({
        id: 'selected-feature-layer',
        type: 'line',
        source: 'selected-feature',
        paint: {
          'line-color': '#f43f5e',
          'line-width': 6,
          'line-dasharray': [0.5, 1.2],
        },
      })

      map.addLayer({
        id: 'draft-feature-layer',
        type: 'line',
        source: 'draft-feature',
        paint: {
          'line-color': '#f97316',
          'line-width': 4,
          'line-dasharray': [1, 1],
        },
      })

      map.on('click', (event) => {
        const clicked = event.lngLat
        const point = [clicked.lng, clicked.lat]

        if (selectedTool === 'point') {
          const nextFeature = {
            id: `draft-point-${Date.now()}`,
            feature_type: selectedFeatureType,
            name: selectedFeatureType.charAt(0).toUpperCase() + selectedFeatureType.slice(1),
            geometry_data: { type: 'Point', coordinates: point },
            properties: { source: 'admin' },
            is_active: true,
            source: 'admin',
          }
          setFeatures((prev) => [...prev, nextFeature])
          setPendingChanges((prev) => [...prev, { operation: 'create', feature: { ...nextFeature, id: undefined } }])
          setMessage({ type: 'success', text: 'Point drafted. Save to apply it.' })
          setSelectedTool('select')
          return
        }

        if (selectedTool === 'line' || selectedTool === 'area') {
          setDraftPoints((prev) => [...prev, point])
          return
        }

        if (selectedTool === 'delete') {
          const feature = features.find((item) => item.id === selectedFeatureId)
          if (feature) {
            setPendingChanges((prev) => [...prev, { operation: 'delete', feature_id: feature.id }])
            setFeatures((prev) => prev.filter((item) => item.id !== feature.id))
            setSelectedFeatureId(null)
            setMessage({ type: 'warning', text: 'Delete scheduled. Save to commit it.' })
          }
          return
        }

        const feature = map.queryRenderedFeatures(event.point, {
          layers: ['custom-roads', 'custom-buildings', 'custom-points'],
        })[0]

        if (feature) {
          const id = feature.properties?.id || feature.id
          setSelectedFeatureId(id)
          setSelectedTool('edit')
        }
      })
    })

    return () => {
      if (mapRef.current) {
        mapRef.current.remove()
        mapRef.current = null
      }
    }
  }, [features, selectedFeatureId, selectedTool, selectedFeatureType])

  const commitFeatureState = useCallback((nextFeatures) => {
    setFeatures(nextFeatures)
    checkpoint(nextFeatures)
  }, [checkpoint])

  const handleSave = useCallback(async () => {
    if (pendingChanges.length === 0) {
      setMessage({ type: 'info', text: 'No unsaved changes to save.' })
      return
    }

    try {
      setLoading(true)
      const response = await saveMapEditorChanges({
        base_map_version: baseMapVersion,
        changes: pendingChanges,
      })

      if (!response.success) {
        throw new Error(response.error?.message || 'Save failed.')
      }

      setBaseMapVersion(response.data.map_version)
      setPendingChanges([])
      setDraftPoints([])
      setMessage({ type: 'success', text: `Map saved successfully. Version ${response.data.map_version}.` })
      await loadFeatures()
    } catch (err) {
      setMessage({ type: 'error', text: err.message || 'Save failed.' })
    } finally {
      setLoading(false)
    }
  }, [baseMapVersion, pendingChanges, loadFeatures])

  const discardChanges = useCallback(() => {
    setPendingChanges([])
    setDraftPoints([])
    setSelectedFeatureId(null)
    loadFeatures()
    setMessage({ type: 'info', text: 'Local changes discarded.' })
  }, [loadFeatures])

  const undoChange = useCallback(() => {
    setHistoryState((prev) => {
      if (prev.index <= 0) return prev
      const nextIndex = prev.index - 1
      const nextFeatures = prev.items[nextIndex]
      setFeatures(nextFeatures)
      return { items: prev.items, index: nextIndex }
    })
  }, [])

  const redoChange = useCallback(() => {
    setHistoryState((prev) => {
      if (prev.index >= prev.items.length - 1) return prev
      const nextIndex = prev.index + 1
      const nextFeatures = prev.items[nextIndex]
      setFeatures(nextFeatures)
      return { items: prev.items, index: nextIndex }
    })
  }, [])

  const updateSelectedFeature = useCallback((patch) => {
    if (!selectedFeatureId) return

    const updated = features.map((feature) => {
      if (feature.id !== selectedFeatureId) return feature
      return { ...feature, ...patch }
    })

    setPendingChanges((prev) => [
      ...prev,
      { operation: 'update', feature_id: selectedFeatureId, changes: patch },
    ])
    commitFeatureState(updated)
  }, [features, selectedFeatureId, commitFeatureState])

  const finishDraft = useCallback(() => {
    if (draftPoints.length < 1) {
      setMessage({ type: 'error', text: 'Add at least one point before finishing.' })
      return
    }

    const geometry = selectedTool === 'area'
      ? { type: 'Polygon', coordinates: [draftPoints.concat([draftPoints[0]])] }
      : { type: 'LineString', coordinates: draftPoints }

    const newFeature = {
      id: `draft-${Date.now()}`,
      feature_type: selectedFeatureType,
      name: `${FEATURE_TYPES.find((item) => item.value === selectedFeatureType)?.label || 'Feature'} ${new Date().getTime().toString().slice(-4)}`,
      geometry_data: geometry,
      properties: { source: 'admin' },
      is_active: true,
      source: 'admin',
    }

    const nextFeatures = [...features, newFeature]
    setPendingChanges((prev) => [...prev, { operation: 'create', feature: { ...newFeature, id: undefined } }])
    commitFeatureState(nextFeatures)
    setDraftPoints([])
    setSelectedFeatureId(newFeature.id)
    setSelectedTool('edit')
    setMessage({ type: 'success', text: 'Draft geometry created. Save to apply it.' })
  }, [draftPoints, selectedTool, selectedFeatureType, features, commitFeatureState])

  useEffect(() => {
    const handleKey = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z' && !event.shiftKey) {
        event.preventDefault()
        undoChange()
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'y') {
        event.preventDefault()
        redoChange()
      }
      if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === 'z') {
        event.preventDefault()
        redoChange()
      }
    }

    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [undoChange, redoChange])

  if (accessState.loading) {
    return <div className="map-editor"><p>Checking admin access...</p></div>
  }

  if (!accessState.allowed) {
    return (
      <div className="map-editor">
        <div className="editor-header">
          <h2>CampusFlow Admin Map Editor</h2>
        </div>
        <div className="alert alert-error">
          {accessState.error || 'Admin access required to edit the campus map.'}
        </div>
        <p>
          <a href="http://localhost:8000/admin/login/?next=/admin/map-editor" target="_blank" rel="noreferrer">
            Log in as an admin
          </a>
        </p>
      </div>
    )
  }

  return (
    <div className="map-editor">
      <div className="editor-header">
        <h2>CampusFlow Admin Map Editor</h2>
      </div>

      <div className="editor-toolbar">
        {TOOL_OPTIONS.map((tool) => (
          <button
            key={tool.value}
            className={selectedTool === tool.value ? 'active' : ''}
            onClick={() => {
              setSelectedTool(tool.value)
              if (tool.value !== 'line' && tool.value !== 'area') setDraftPoints([])
            }}
          >
            {tool.label}
          </button>
        ))}
      </div>

      {message && (
        <div className={`alert alert-${message.type}`}>
          {message.text}
        </div>
      )}

      <div className="editor-layout">
        <div className="editor-map-wrap">
          <div ref={mapContainer} className="campus-map" />
          {selectedTool === 'line' || selectedTool === 'area' ? (
            <div className="editor-draft-actions">
              <button onClick={finishDraft}>Finish Drawing</button>
              <button onClick={() => setDraftPoints([])}>Cancel</button>
            </div>
          ) : null}
        </div>

        <aside className="editor-inspector">
          <h3>Selected Feature</h3>
          {selectedFeature ? (
            <>
              <p>ID: {selectedFeature.id}</p>
              <div className="form-group">
                <label>Type</label>
                <select value={selectedFeature.feature_type} onChange={(event) => updateSelectedFeature({ feature_type: event.target.value })}>
                  {FEATURE_TYPES.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </div>
              <div className="form-group">
                <label>Name</label>
                <input value={selectedFeature.name || ''} onChange={(event) => updateSelectedFeature({ name: event.target.value })} />
              </div>
              <div className="form-group">
                <label>Source</label>
                <input value={selectedFeature.source || 'admin'} onChange={(event) => updateSelectedFeature({ source: event.target.value })} />
              </div>
              <div className="toggle-row">
                <label><input type="checkbox" checked={selectedFeature.properties?.walkable ?? false} onChange={(event) => updateSelectedFeature({ properties: { ...selectedFeature.properties, walkable: event.target.checked } })} /> Walkable</label>
              </div>
              <div className="toggle-row">
                <label><input type="checkbox" checked={selectedFeature.properties?.one_way ?? false} onChange={(event) => updateSelectedFeature({ properties: { ...selectedFeature.properties, one_way: event.target.checked } })} /> One way</label>
              </div>
              <div className="form-group">
                <label>Road type</label>
                <input value={selectedFeature.properties?.road_type || ''} onChange={(event) => updateSelectedFeature({ properties: { ...selectedFeature.properties, road_type: event.target.value } })} />
              </div>
              <div className="form-group">
                <label>Speed (km/h)</label>
                <input type="number" value={selectedFeature.properties?.speed_kmh ?? 5} onChange={(event) => updateSelectedFeature({ properties: { ...selectedFeature.properties, speed_kmh: Number(event.target.value) } })} />
              </div>
              <button className="btn btn-danger" onClick={() => {
                setPendingChanges((prev) => [...prev, { operation: 'delete', feature_id: selectedFeature.id }])
                setFeatures((prev) => prev.filter((item) => item.id !== selectedFeature.id))
                setSelectedFeatureId(null)
              }}>
                Delete Feature
              </button>
            </>
          ) : (
            <div className="empty-state">
              <p>Select a feature on the map to inspect or edit it.</p>
              <select value={selectedFeatureType} onChange={(event) => setSelectedFeatureType(event.target.value)}>
                {FEATURE_TYPES.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </div>
          )}
        </aside>
      </div>

      <div className="editor-footer">
        <div>Unsaved changes: {unsavedCount}</div>
        <div className="editor-actions">
          <button onClick={undoChange}>Undo</button>
          <button onClick={redoChange}>Redo</button>
          <button onClick={discardChanges}>Discard</button>
          <button className="btn btn-primary" onClick={handleSave} disabled={loading || unsavedCount === 0}>SAVE CHANGES</button>
        </div>
      </div>
    </div>
  )
}
