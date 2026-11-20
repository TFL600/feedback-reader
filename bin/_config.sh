# Sourced by the bin/ scripts: loads FR_* and WHISPER_* settings from ~/.feedback-reader/config.env
# (or $FR_CONFIG) without overriding anything already set in the environment. See reader/config.py.
_fr_cfg="${FR_CONFIG:-$HOME/.feedback-reader/config.env}"
if [ -f "$_fr_cfg" ]; then
	while IFS= read -r _l || [ -n "$_l" ]; do
		_l="${_l#"${_l%%[![:space:]]*}"}"
		case "$_l" in '' | '#'*) continue ;; esac
		_l="${_l#export }"
		_k="${_l%%=*}"; _v="${_l#*=}"
		case "$_k" in FR_* | WHISPER_*) ;; *) continue ;; esac
		_v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"
		case "$_v" in "~/"*) _v="$HOME/${_v#\~/}" ;; esac
		[ -n "${!_k:-}" ] || export "$_k=$_v"
	done < "$_fr_cfg"
fi
unset _fr_cfg _l _k _v
