"""Sign-in for the one user: password + TOTP, then a JWT in an httpOnly cookie
(ARCHITECTURE §9, D11, D44). Every API route needs it unless it's listed as public."""
