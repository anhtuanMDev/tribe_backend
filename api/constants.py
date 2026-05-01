class VerificationPurpose:
    REGISTER = "register"
    RESET_PASSWORD = "reset_password"
    DELETE_ACCOUNT = "delete_account"
    CHANGE_PASSWORD = "change_password"


PURPOSE_CHOICES = {
    VerificationPurpose.REGISTER: "Register",
    VerificationPurpose.RESET_PASSWORD: "Reset Password",
    VerificationPurpose.DELETE_ACCOUNT: "Delete Account",
    VerificationPurpose.CHANGE_PASSWORD: "Change Password",
}

ACTIVITIES = [
    ("Soccer", "soccer"),
    ("Badminton", "badminton"),
    ("Volleyball", "volleyball"),
    ("Basketball", "basketball"),
    ("Swimming", "swimming"),
    ("Hiking", "hiking"),
    ("Jogging", "jogging"),
    ("Cycling", "cycling"),
    ("Travel", "travel"),
    ("Gaming", "gaming"),
    ("Racing", "racing"),
    ("Other", "other"),
]
