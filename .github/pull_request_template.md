<!-- Thanks for contributing! A short PR is easier to review than a long one. -->

**What this does**
A sentence or two on the change and why.

**Related issue**
Closes #…

**Checklist**
- [ ] `python -m compileall playlisterr` passes
- [ ] No third-party imports added to the core (standard library only)
- [ ] No build step introduced for the web UI
- [ ] If I changed a setting, I regenerated `config.example.json`
      (`python -m playlisterr config --write-example config.example.json`) and
      updated the README/docs
- [ ] I didn't commit any fixtures recorded against a real server
