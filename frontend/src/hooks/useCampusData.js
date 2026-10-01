/**
 * Hook for loading campus map data and places.
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { getMapData, getMapVersion } from '../services/mapApi'
import { getPlaces } from '../services/placeApi'

export function useCampusData() {
  const [mapData, setMapData] = useState(null)
  const [places, setPlaces] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const versionRef = useRef(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      const [mapResult, placesResult] = await Promise.all([
        getMapData(),
        getPlaces({ limit: 100 }),
      ])

      const nextVersion = mapResult?.data?.map_version ?? versionRef.current ?? 0
      versionRef.current = nextVersion
      setMapData(mapResult.data)
      setPlaces(placesResult.data || [])
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let cancelled = false

    const refreshIfNeeded = async () => {
      try {
        const versionResult = await getMapVersion()
        if (cancelled || !versionResult?.success) return

        const incomingVersion = versionResult.data?.map_version ?? 0
        if (versionRef.current !== null && incomingVersion !== versionRef.current) {
          await load()
          return
        }
        versionRef.current = incomingVersion
      } catch (err) {
        if (!cancelled) {
          setError(err.message)
        }
      }
    }

    load()
    const intervalId = setInterval(refreshIfNeeded, 10000)

    return () => {
      cancelled = true
      clearInterval(intervalId)
    }
  }, [load])

  return { mapData, places, loading, error }
}
