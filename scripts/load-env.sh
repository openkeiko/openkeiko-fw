#!/usr/bin/env bash

load_openkeiko_env() {
  local env_file=${1:-.env}
  local key value

  [[ -f $env_file ]] || return 0

  while IFS='=' read -r key value || [[ -n $key ]]; do
    key=${key#"${key%%[![:space:]]*}"}
    key=${key%"${key##*[![:space:]]}"}
    [[ -z $key || $key == \#* ]] && continue

    case $key in
      FW1_MAIN_SERIAL|FW1_DISPLAY_SERIAL) ;;
      *) continue ;;
    esac

    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    if [[ $value == \"*\" && $value == *\" ]]; then
      value=${value:1:${#value}-2}
    elif [[ $value == \'*\' && $value == *\' ]]; then
      value=${value:1:${#value}-2}
    fi

    if [[ -z ${!key:-} ]]; then
      printf -v "$key" '%s' "$value"
      export "$key"
    fi
  done < "$env_file"
}
