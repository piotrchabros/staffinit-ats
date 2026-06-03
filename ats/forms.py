from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Company, Deal, Person, Role


class NewUserForm(UserCreationForm):
    """Provision a new login (superuser-only flow).

    Extends Django's UserCreationForm — so the two-password confirmation and the
    project's AUTH_PASSWORD_VALIDATORS still apply — adding an optional email and
    an "admin" flag that grants user-management (and Django admin) access.
    """

    email = forms.EmailField(required=False)
    is_admin = forms.BooleanField(
        required=False,
        label="Can manage users",
        help_text="Administrators can add and remove other users.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get("email", "")
        if self.cleaned_data.get("is_admin"):
            # is_staff too, so admins keep access to the Django admin.
            user.is_superuser = True
            user.is_staff = True
        if commit:
            user.save()
        return user


class RoleForm(forms.ModelForm):
    class Meta:
        model = Role
        fields = ["title", "client", "jd_text"]
        widgets = {
            "jd_text": forms.Textarea(attrs={"rows": 12, "placeholder": "Paste the job description"}),
        }


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A FileField that accepts several files (Django dropped multiple support
    from the default widget; this is the documented re-add)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data]
        return [single(data, initial)] if data else []


class AddCandidateForm(forms.Form):
    """Add candidate(s) to a role. Drop one or more CV files — name + email are
    auto-extracted from each CV. The fields below are optional overrides / a
    fallback when a file can't be read or has no email.
    """

    cv_files = MultipleFileField(required=False, help_text="PDF or DOCX — drop several at once.")
    pasted_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 6, "placeholder": "…or paste a single CV's text"}),
    )
    full_name = forms.CharField(max_length=255, required=False)
    email = forms.EmailField(required=False)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("cv_files") and not cleaned.get("pasted_text", "").strip():
            raise forms.ValidationError("Drop at least one CV file, or paste CV text.")
        return cleaned


class PasteTextForm(forms.Form):
    """Manual-paste fallback for a CV whose file could not be parsed."""

    parsed_text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 10, "placeholder": "Paste the CV text"}),
    )


# --------------------------------------------------------------------------- #
# Mini-CRM forms                                                              #
# --------------------------------------------------------------------------- #
class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ["name", "website", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class PersonForm(forms.ModelForm):
    """A contact at a company. The company is set by the view, not the form."""

    class Meta:
        model = Person
        fields = ["full_name", "title", "email", "phone", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class DealForm(forms.ModelForm):
    """A signed placement. The company is set by the view, not the form."""

    class Meta:
        model = Deal
        fields = [
            "developer_name", "role_title", "salary", "client_rate",
            "currency", "signed_date", "notes",
        ]
        widgets = {
            "signed_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
