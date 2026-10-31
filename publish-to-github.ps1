# Publishes this SwarmForge folder to https://github.com/bigdan88/swarmforge
# Run from PowerShell:  .\publish-to-github.ps1
# (If PowerShell blocks scripts:  powershell -ExecutionPolicy Bypass -File .\publish-to-github.ps1)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Remote = "https://github.com/bigdan88/swarmforge.git"

# 1. Safety: never publish secrets or private notes
$tracked = git ls-files
foreach ($bad in @('.env', 'swarmforge.yaml')) {
    if ($tracked -contains $bad) { throw "$bad is tracked by git - remove it (git rm --cached $bad) before publishing." }
}
if ($tracked -match '^knowledge/') { throw "knowledge/ is tracked by git - remove it before publishing." }

# 2. Commit anything not yet committed
if (git status --porcelain) {
    Write-Host "Committing uncommitted changes..."
    git add -A
    git commit -m "Update before publishing to GitHub"
}

# 3. Use the GitHub CLI login for git if available (otherwise Git Credential Manager opens a browser login)
if (Get-Command gh -ErrorAction SilentlyContinue) {
    gh auth status 2>$null
    if ($LASTEXITCODE -ne 0) { gh auth login }
    gh auth setup-git
}

# 4. Point 'origin' at the GitHub repo and push main
if ((git remote) -contains 'origin') { git remote set-url origin $Remote } else { git remote add origin $Remote }
git push -u origin main
if ($LASTEXITCODE -ne 0) { throw "Push failed (see the message above)." }

Write-Host ""
Write-Host "Done: https://github.com/bigdan88/swarmforge" -ForegroundColor Green
Write-Host "SwarmForge can now work on itself from the dashboard with repository: bigdan88/swarmforge"
