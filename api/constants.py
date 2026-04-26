class VerificationPurpose:
    REGISTER = "register"
    RESET_PASSWORD = "reset_password"
    DELETE_ACCOUNT = "delete_account"


PURPOSE_CHOICES = {
    VerificationPurpose.REGISTER: "Register",
    VerificationPurpose.RESET_PASSWORD: "Reset Password",
    VerificationPurpose.DELETE_ACCOUNT: "Delete Account",
}
