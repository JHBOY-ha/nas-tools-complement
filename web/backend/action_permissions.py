"""Server-side action policy shared by browser sessions and user JWT APIs."""

# Root-only commands can change credentials, code, or global service settings.
# Every dispatcher command must be explicitly listed; future additions fail closed.
_COMMAND_GROUPS = {
    # These status routes perform original-command and owner checks themselves.
    "": ("logout", "version", "refresh_process", 'get_action_task', 'get_action_tasks',
          'find_action_task', 'cancel_action_task'),
    "@admin": (
        "user_manager", "get_users", "restart", "update_system", "reset_db_version",
        "update_config", "update_directory", "test_connection", "restory_backup",
        "set_system_config", "save_user_script", "add_downloader", "delete_downloader",
        "get_downloader", "update_message_client", "delete_message_client",
        "check_message_client", "get_message_client", "test_message_client",
        "openclaw_qr_start", "openclaw_qr_status", "refresh_message", "test_opensubtitles",
        "update_download_setting", "delete_download_setting", "cookiecloud_sync",
        "add_or_edit_sync_path", "delete_sync_path", "check_sync_path",
    ),
    "资源搜索": ("search", "get_search_result", "download", "download_link", "download_torrent"),
    "下载管理": ("pt_start", "pt_stop", "pt_remove", "pt_info", "get_downloaded", "get_downloading",
                 "update_torrent_remove_task", "get_torrent_remove_task", "delete_torrent_remove_task",
                 "get_remove_torrents", "auto_remove_torrents"),
    "媒体整理": (
        "del_unknown_path", "rename", "rename_udf", "delete_history", "re_identification",
        "special_confirmation", "truncate_blacklist", "get_transfer_statistics", "get_transfer_history",
        "get_unknown_list", "rename_file", "delete_files", "download_subtitle", "find_hardlinks",
        "run_directory_sync", "start_mediasync", "mediasync_state",
    ),
    ("媒体整理", "系统设置"): (
        "get_sync_path", "get_directorysync",
        "add_custom_word_group", "delete_custom_word_group", "add_or_edit_custom_word", "get_custom_word",
        "delete_custom_word", "check_custom_words", "export_custom_words", "analyse_import_custom_words_code",
        "import_custom_words", "get_customwords",
    ),
    "站点管理": (
        "update_site", "get_site", "del_site", "get_site_favicon", "get_site_activity", "get_site_history",
        "get_site_seeding_info", "get_site_user_statistics", "check_site_attr", "test_site",
        "update_sites_cookie_ua", "set_site_captcha_code", "add_brushtask", "del_brushtask",
        "brushtask_detail", "run_brushtask", "list_brushtask_torrents",
    ),
    "订阅管理": (
        "remove_rss_media", "add_rss_media", "refresh_rss", "rss_detail", "truncate_rsshistory",
        "get_userrss_task", "delete_userrss_task", "update_userrss_task", "get_rssparser",
        "delete_rssparser", "update_rssparser", "run_userrss", "list_rss_articles", "list_rss_history",
        "rss_article_test", "rss_articles_check", "rss_articles_download", "re_rss_history",
        "delete_rss_history", "get_rss_history", "get_movie_rss_list", "get_tv_rss_list",
        "get_douban_history", "delete_douban_history",
    ),
    "探索": ("get_recommend", "media_similar", "media_recommendations", "media_person", "person_medias"),
    # 出口 IP 查询与站点连通性测试沿用相同的服务权限。
    "服务": ("sch", "name_test", "rule_test", "net_test", "egress_ip_test", "speed_test", "logging",
             "clear_tmdb_cache", "delete_tmdb_cache", "modify_tmdb_cache", "send_custom_message"),
    "系统设置": (
        "add_filtergroup", "restore_filtergroup", "set_default_filtergroup", "del_filtergroup",
        "add_filterrule", "del_filterrule", "filterrule_detail", "share_filtergroup", "import_filtergroup",
    ),
    ("我的媒体库", "探索", "订阅管理"): ("movie_calendar_data", "tv_calendar_data", "media_detail"),
    ("我的媒体库", "媒体整理"): ("get_library_spacesize", "get_library_mediacount", "get_library_playhistory"),
    ("资源搜索", "站点管理"): ("list_site_resources",),
    ("资源搜索", "站点管理", "订阅管理", "系统设置"): ("get_sites", "get_indexers", "get_filterrules"),
    ("资源搜索", "下载管理", "订阅管理", "媒体整理", "系统设置"): ("get_download_setting", "get_download_dirs"),
    ("媒体整理", "资源搜索", "订阅管理", "探索", "服务", "系统设置"): (
        "media_info", "search_media_infos", "get_tvseason_list", "get_categories", "get_sub_path"),
}
ACTION_PERMISSIONS = {command: permission for permission, commands in _COMMAND_GROUPS.items() for command in commands}

# These native routes render credentials/configuration or bypass the dispatcher.
# Protect their views as well as /do, so a backup/page cannot leak the API key.
PAGE_ACTIONS = {
    "resources": "list_site_resources",  # Reject denied pages before rendering empty results.
    "basic": "update_config", "douban": "update_config", "downloader": "update_config",
    "indexer": "update_config", "library": "update_config", "mediaserver": "update_config",
    "notification": "get_message_client", "subtitle": "update_config", "users": "user_manager",
    "backup": "restory_backup", "upload": "restory_backup", "userdownloader": "get_downloader",
    "download_setting": "update_download_setting", "sites": "get_site", "sitelist": "get_sites",
    "brushtask": "brushtask_detail", "customwords": "get_customwords", "directorysync": "add_or_edit_sync_path",
    "dirlist": "get_download_dirs", "subscribe": "add_rss_media", "user_rss": "get_userrss_task",
    "rss_parser": "get_rssparser", "filterrule": "get_filterrules",
}


def action_allowed(user, command):
    """Use actual session identity; possessing all menu labels does not make root."""
    if not isinstance(command, str) or command not in ACTION_PERMISSIONS or not user or not user.is_authenticated:
        return False
    if str(user.get_id()) == "0":
        return True
    required = ACTION_PERMISSIONS[command]
    if required == "@admin":
        return False
    if not required:
        return True
    granted = {value.strip() for value in str(getattr(user, "pris", "") or "").split(",")}
    return bool(granted.intersection(required if isinstance(required, tuple) else (required,)))
