"""Page renderer with explicit dependencies supplied by the entrypoint."""

def render(cfg, st):
    st.caption("Edit swarmforge.yaml, run health checks and test models.")
    from swarmforge import settings
    settings.render(cfg)
