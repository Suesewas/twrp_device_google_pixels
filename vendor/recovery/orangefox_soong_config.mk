# OrangeFox Soong build-var bridge
#
# Soong (the .go *_defaults under bootable/recovery) reads OrangeFox feature
# flags through getMakeVars(), i.e. the "twrpVarsPlugin" SOONG_CONFIG namespace.
# Soong cannot see plain Make variables, so a flag set as "OF_FOO := 1" in a
# device tree .mk only reaches Soong if it is exported into that namespace here.
#
# This list lives in vendor/recovery (OrangeFox-owned) rather than in
# vendor/twrp/config/BoardConfigSoong.mk so that the upstream TWRP config stays
# untouched. It is included from build/make/core/config.mk, right after
# BoardConfigTWRP.mk (which creates the twrpVarsPlugin namespace and runs after
# the device BoardConfig, so the OF_* values are already set by the time we read
# them below).

# Some settings flags are canonically FOX_*-prefixed: OrangeFox_A16.sh and the
# installer (installer/META-INF/com/google/android/update-binary) read them as
# FOX_* at build/flash time, so the FOX_* name must be left intact. Soong,
# however, reads them under OF_*. Mirror the FOX_* value onto OF_* (only when
# OF_* isn't already set) so a build that sets the documented FOX_* name still
# reaches Soong. This is NOT a deprecation: FOX_* remains the primary name for
# these vars.
OF_MIRRORED_FROM_FOX := \
    USE_NANO_EDITOR \
    ALLOW_EARLY_SETTINGS_LOAD \
    SETTINGS_ROOT_DIRECTORY \
    MISCELLANEOUS_ROOT_DIRECTORY \
    USE_DATA_RECOVERY_FOR_SETTINGS \
    USE_MEIZU_TOUCH_MAPPING

define fox_mirror_var
ifneq ($$(FOX_$(1)),)
  OF_$(1) ?= $$(FOX_$(1))
endif
endef

$(foreach v,$(OF_MIRRORED_FROM_FOX),$(eval $(call fox_mirror_var,$(v))))

# Bridge the OF_* feature flags into the twrpVarsPlugin Soong namespace so they
# take effect when declared in a device .mk, not only when exported to the
# environment. getMakeVars() in the recovery *_defaults reads exactly these.
$(call add_soong_config_var,twrpVarsPlugin,\
    OF_ENABLE_WLAN \
    OF_ENABLE_LAB \
    OF_LANDSCAPE_MODE \
    OF_SUPPORT_OZIP_DECRYPTION \
    OF_USE_NANO_EDITOR \
    OF_ALLOW_EARLY_SETTINGS_LOAD \
    OF_SETTINGS_ROOT_DIRECTORY \
    OF_MISCELLANEOUS_ROOT_DIRECTORY \
    OF_USE_DATA_RECOVERY_FOR_SETTINGS \
    OF_USE_MEIZU_TOUCH_MAPPING)
