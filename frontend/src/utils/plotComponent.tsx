/**
 * Plotly React component factory.
 *
 * Uses `plotly.js-basic-dist` (~1 MB) instead of the full `plotly.js`
 * (~3 MB). All our plotters use only basic chart types (scatter), so we
 * don't need the heavy 3D / mapbox / finance / etc. distributions.
 *
 * `react-plotly.js` ships a `factory` that takes any Plotly constructor,
 * which is how we plug in the basic distribution. See:
 * https://github.com/plotly/react-plotly.js#custom-plotly-build
 */
import createPlotlyComponent from "react-plotly.js/factory"
import Plotly from "plotly.js-basic-dist"

export default createPlotlyComponent(Plotly)
