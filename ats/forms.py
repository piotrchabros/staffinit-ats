from django import forms

from .models import Role


class RoleForm(forms.ModelForm):
    class Meta:
        model = Role
        fields = ["title", "client", "jd_text"]
        widgets = {
            "jd_text": forms.Textarea(attrs={"rows": 12, "placeholder": "Paste the job description"}),
        }


class AddCandidateForm(forms.Form):
    """Add a candidate to a role: name + email, plus either a CV file OR pasted text."""

    full_name = forms.CharField(max_length=255)
    email = forms.EmailField()
    cv_file = forms.FileField(required=False, help_text="PDF or DOCX")
    pasted_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 8, "placeholder": "...or paste the CV text"}),
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("cv_file") and not cleaned.get("pasted_text", "").strip():
            raise forms.ValidationError("Upload a CV file or paste the CV text.")
        return cleaned


class PasteTextForm(forms.Form):
    """Manual-paste fallback for a CV whose file could not be parsed."""

    parsed_text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 10, "placeholder": "Paste the CV text"}),
    )
