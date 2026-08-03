import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Run an async function and track loading / error / data in one place.
 *
 * Requests are aborted when the inputs change or the component unmounts, so a
 * slow response cannot overwrite newer state.
 */
export function useAsync(fn, deps, { immediate = true } = {}) {
  const [state, setState] = useState({
    data: null,
    error: null,
    loading: immediate,
  })
  const controllerRef = useRef(null)
  // Kept in a ref so `run` stays stable while still calling the latest fn.
  const fnRef = useRef(fn)
  fnRef.current = fn

  const run = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller

    setState((previous) => ({ ...previous, loading: true, error: null }))
    try {
      const data = await fnRef.current(controller.signal)
      if (!controller.signal.aborted) {
        setState({ data, error: null, loading: false })
      }
      return data
    } catch (error) {
      if (controller.signal.aborted || error?.name === 'AbortError') return null
      setState({ data: null, error, loading: false })
      return null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (immediate) run()
    return () => controllerRef.current?.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { ...state, run, setData: (data) => setState({ data, error: null, loading: false }) }
}
