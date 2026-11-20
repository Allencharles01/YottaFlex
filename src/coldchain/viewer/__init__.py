"""Read-only streamlit viewer of the pipeline output (Phase 9, ``app/streamlit_app.py``).

* :mod:`coldchain.viewer.data` finds and reads the existing files under ``output/<scale>/``
  (through :mod:`coldchain.io`; it never writes and never runs a pipeline stage);
* :mod:`coldchain.viewer.views` turns tables into what a page shows (pure functions);
* :mod:`coldchain.viewer.charts` builds the plotly figures (pure functions);
* :mod:`coldchain.viewer.session` is the streamlit glue: scale selector, caching, the
  "missing output" message and the shared shipment selection. It is the only module here that
  imports streamlit (the optional ``dashboard`` extra).
"""
