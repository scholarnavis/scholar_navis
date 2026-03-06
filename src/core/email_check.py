from email_validator import validate_email, EmailNotValidError
from disposable_email_domains import blocklist as DISPOSABLE_BLOCKLIST


ROLE_PREFIXES = {
    "admin", "support", "info", "sales", "contact",
    "noreply", "postmaster", "webmaster", "root"
}


CUSTOM_DOMAIN_BLOCKLIST = {
    "example.com",
    "university.edu"
}


BLOCKED_TLDS = {
    "tk", "ml", "ga", "cf", "gq",
    "xyz", "top", "icu", "pw", "click", "cyou", "vip", "work",
    "surf", "cam", "country", "stream", "download", "xin", "gdn",
    "racing", "jetzt", "win", "bid", "trade", "date", "party",
    "review", "science", "cricket", "site", "online", "space", "fun"
}

def verify_email_robust(email: str, check_mx: bool = True, check_roles: bool = True) -> dict:
    """
    邮箱鉴别函数，集成语法、DNS/MX、开源临时邮箱库及自定义黑名单。

    :param email: 待校验的邮箱字符串
    :param check_mx: 是否进行真实的 DNS/MX 记录探测
    :param check_roles: 是否拦截公共角色邮箱 (如 admin@)
    :return: 包含校验结果和详细错误信息的字典
    """
    result = {
        "is_valid": False,
        "normalized_email": None,
        "error_type": None,
        "error_msg": ""
    }

    # 1基础非空校验
    if not email or not isinstance(email, str):
        result["error_type"] = "empty_or_invalid_type"
        result["error_msg"] = "The email address cannot be empty."
        return result

    email = email.strip()

    # 角色邮箱拦截
    if check_roles and '@' in email:
        local_part = email.split('@')[0].lower()
        if local_part in ROLE_PREFIXES:
            result["error_type"] = "role_account"
            result["error_msg"] = f"Public or system-reserved email addresses are not allowed: {local_part}@...)。"
            return result

    # 语法及 DNS/MX 记录校验
    try:
        valid_email_info = validate_email(email, check_deliverability=check_mx)
        normalized_email = valid_email_info.normalized
        domain_part = valid_email_info.domain
    except EmailNotValidError as e:
        result["error_type"] = "syntax_or_dns_error"
        result["error_msg"] = f"Invalid email addresses or domains cannot receive emails: {str(e)}"
        return result

    # 4. 虚拟/一次性邮箱拦截
    if domain_part in DISPOSABLE_BLOCKLIST:
        result["error_type"] = "disposable_email"
        result["error_msg"] = "The system refuses registration using temporary or disposable email addresses. Please use a regular email address."
        return result

    # 5. 业务自定义黑名单拦截
    if domain_part in CUSTOM_DOMAIN_BLOCKLIST:
        result["error_type"] = "custom_blocked_domain"
        result["error_msg"] = f"The email domain ({domain_part}) is on the restricted list of this system."
        return result

    # 6. 自定义黑名单拦截
    if domain_part in CUSTOM_DOMAIN_BLOCKLIST:
        result["error_type"] = "custom_blocked_domain"
        result["error_msg"] = f"The email domain ({domain_part}) is on the restricted list of this system."
        return result

    # 校验全部通过
    result["is_valid"] = True
    result["normalized_email"] = normalized_email
    return result