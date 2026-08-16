# Status line for printvendo-backend.
#
# Rendered on every prompt, so it must be cheap: it reads two small files and
# asks git for the branch name. It never runs the test suite -- a status line
# that took 70 seconds would be a status line nobody keeps.
#
# The counts come from .claude/build-state.json, which is written after a real
# verification run. That is deliberate: the number shown is one that was
# actually observed, not one computed from something that correlates with it.

$ErrorActionPreference = 'SilentlyContinue'

try { $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json } catch { $payload = $null }

$root = $payload.workspace.current_dir
if (-not $root) { $root = $payload.cwd }
if (-not $root) { $root = (Get-Location).Path }

# ── segments ────────────────────────────────────────────────────────────────

$esc = [char]27
function Paint($text, $code) { "$esc[${code}m$text$esc[0m" }

$parts = @()

$parts += Paint 'CAVEMAN' '38;5;208'

$branch = & git -C $root rev-parse --abbrev-ref HEAD 2>$null
if ($branch) {
    $dirty = & git -C $root status --porcelain 2>$null
    $mark = if ($dirty) { '*' } else { '' }
    $parts += Paint "$branch$mark" '38;5;110'
}

$statePath = Join-Path $root '.claude/build-state.json'
if (Test-Path $statePath) {
    $state = Get-Content $statePath -Raw | ConvertFrom-Json

    $parts += Paint "$($state.tests) tests" '38;5;114'
    $parts += Paint "$($state.contracts) contracts" '38;5;114'

    $doneCount = @($state.done).Count
    $total = $doneCount + @($state.remaining).Count
    $parts += Paint "$doneCount/$total modules" '38;5;180'
    $parts += Paint "next: $($state.next)" '38;5;245'
}

($parts -join (Paint ' | ' '38;5;240'))
