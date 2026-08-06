#pragma once

#include <cctype>
#include <cstdio>
#include <cstring>
#include <string>

enum class SoilSettingAction {
  INVALID,
  SET,
  RESET,
};

struct SoilSettingCommand {
  SoilSettingAction action{SoilSettingAction::INVALID};
  char key[32]{};
  char value[16]{};
};

constexpr bool soil_reported_rewake_backoff(bool enabled, int wake_count) {
  return enabled && wake_count >= 5;
}

constexpr bool soil_failed_rewake_backoff(int failed_wake_count) {
  return failed_wake_count >= 10;
}

inline void soil_uppercase_token(char *token) {
  for (; *token != '\0'; ++token) {
    *token = static_cast<char>(
        std::toupper(static_cast<unsigned char>(*token)));
  }
}

// Commands use a stable, extensible grammar:
//   SET <setting> <value>
//   RESET <setting>
// Extra tokens are rejected so malformed commands cannot be partly applied.
inline SoilSettingCommand soil_parse_setting_command(const std::string &input) {
  SoilSettingCommand command;
  char action[8]{};
  char extra = '\0';
  const int token_count = std::sscanf(
      input.c_str(), " %7s %31s %15s %c", action, command.key,
      command.value, &extra);

  soil_uppercase_token(action);
  soil_uppercase_token(command.key);
  soil_uppercase_token(command.value);

  if (std::strcmp(action, "SET") == 0 && token_count == 3) {
    command.action = SoilSettingAction::SET;
  } else if (std::strcmp(action, "RESET") == 0 && token_count == 2) {
    command.action = SoilSettingAction::RESET;
  }
  return command;
}
