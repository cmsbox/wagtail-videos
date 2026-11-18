from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from wagtail.admin import messages
from wagtail.admin.auth import PermissionPolicyChecker
from wagtail.admin.utils import get_valid_next_url_from_request
from wagtail.admin.filters import BaseMediaFilterSet
from wagtail.admin.views import generic
from wagtail.search.backends import get_search_backends

from wagtailvideos import get_transcoder_backend, get_video_model
from wagtailvideos.forms import VideoTranscodeAdminForm, get_video_form
from wagtailvideos.permissions import permission_policy

permission_checker = PermissionPolicyChecker(permission_policy)


Video = get_video_model()


class VideoFilterSet(BaseMediaFilterSet):
    permission_policy = permission_policy

    class Meta:
        model = Video
        fields = []


class IndexView(generic.IndexView):
    context_object_name = "videos"
    model = Video
    filterset_class = VideoFilterSet
    permission_policy = permission_policy
    any_permission_required = ['add', 'change', 'delete']
    header_icon = 'media'
    template_name = 'wagtailvideos/videos/index.html'
    results_template_name = 'wagtailvideos/videos/results.html'
    _show_breadcrumbs = True
    add_url_name = "wagtailvideos:add_multiple"
    edit_url_name = "wagtailvideos:edit"
    index_results_url_name = 'wagtailvideos:index_results'
    add_item_label = _("Add a video")

    def get_breadcrumbs_items(self):
        return self.breadcrumbs_items + [
            {"url": "", "label": _("Videos")},
        ]

    def get_filterset_kwargs(self):
        kwargs = super().get_filterset_kwargs()
        kwargs["is_searching"] = self.is_searching
        return kwargs

    def get_paginate_by(self, queryset):
        return 32  # 4 x 8


class EditView(generic.EditView):
    model = Video
    permission_policy = permission_policy
    pk_url_kwarg = "video_id"
    error_message = _("The video could not be saved due to errors.")
    template_name = "wagtailvideos/videos/edit.html"
    index_url_name = "wagtailvideos:index"
    edit_url_name = "wagtailvideos:edit"
    delete_url_name = "wagtailvideos:delete"
    header_icon = "media"
    context_object_name = "video"

    def get_form_class(self):
        return get_video_form(self.model)

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not self.permission_policy.user_has_permission_for_instance(
            self.request.user, "change", obj
        ):
            raise PermissionDenied
        return obj

    def get_success_message(self):
        return _("Video '{0}' updated.").format(self.object.title)

    @cached_property
    def next_url(self):
        return get_valid_next_url_from_request(self.request)

    def get_success_url(self):
        return self.next_url or super().get_success_url()

    def get_delete_url(self):
        delete_url = super().get_delete_url()
        if self.next_url:
            delete_url += "?" + urlencode({"next": self.next_url})
        return delete_url

    def save_instance(self):
        video = self.form.instance
        original_file_name = None
        if "file" in self.form.changed_data:
            original_file_name = (
                self.model.objects.filter(pk=video.pk)
                .values_list("file", flat=True)
                .first()
            )
            video.file_size = video.file.size

        video = super().save_instance()

        if original_file_name and original_file_name != video.file.name:
            # A new video file was uploaded: delete the old one from storage.
            # NB Doing this via original_file.delete() would clear the file field.
            video.file.storage.delete(original_file_name)

        # Reindex the video to make sure all tags are indexed
        for backend in get_search_backends():
            backend.add(video)

        return video

    def render_to_response(self, context, **response_kwargs):
        if not self.object._meta.get_field("file").storage.exists(self.object.file.name):
            messages.error(
                self.request,
                _("The source video file could not be found. Please change the source or delete the video."),
                buttons=[messages.button(self.get_delete_url(), _("Delete"))],
            )
        return super().render_to_response(context, **response_kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        video = self.object
        if hasattr(video, "track_listing"):
            tracks_action_url = reverse("wagtailvideos_tracks:edit", args=(video.track_listing.pk,))
        else:
            tracks_action_url = reverse("wagtailvideos_tracks:add")
        context.update({
            "next": self.next_url,
            "usage_count_val": video.get_usage().count(),
            "filesize": video.get_file_size(),
            "transcoder_installed": get_transcoder_backend().installed(),
            "transcoder_enabled": not getattr(settings, "WAGTAIL_VIDEOS_DISABLE_TRANSCODE", False),
            "transcodes": video.transcodes.all(),
            "transcode_form": VideoTranscodeAdminForm(video=video),
            "tracks_action_url": tracks_action_url,
        })
        return context


@require_POST
def create_transcode(request, video_id):
    video = get_object_or_404(get_video_model(), id=video_id)
    transcode_form = VideoTranscodeAdminForm(data=request.POST, video=video)

    if transcode_form.is_valid():
        transcode_form.save()
    return redirect('wagtailvideos:edit', video_id)


@permission_checker.require('delete')
def delete_transcode(request, video_id, transcode_id):
    video = get_object_or_404(get_video_model(), id=video_id)
    video.transcodes.filter(id=transcode_id).delete()
    return redirect('wagtailvideos:edit', video_id)


class DeleteView(generic.DeleteView):
    model = Video
    permission_policy = permission_policy
    pk_url_kwarg = "video_id"
    permission_required = "delete"
    header_icon = "media"
    template_name = "wagtailvideos/videos/confirm_delete.html"
    # Explicit, otherwise a custom video model would expose e.g. `customvideo`
    # instead of `video` to the template
    context_object_name = "video"
    usage_url_name = "wagtailvideos:video_usage"
    delete_url_name = "wagtailvideos:delete"
    index_url_name = "wagtailvideos:index"
    page_title = _("Delete video")

    def user_has_permission(self, permission):
        return self.permission_policy.user_has_permission_for_instance(
            self.request.user, permission, self.object
        )

    @property
    def confirmation_message(self):
        return _("Are you sure you want to delete this video?")

    def get_success_message(self):
        return _("Video '{0}' deleted.").format(self.object.title)


@permission_checker.require('add')
def add(request):
    Video = get_video_model()
    VideoForm = get_video_form(Video)

    if request.POST:
        video = Video(uploaded_by_user=request.user)
        form = VideoForm(request.POST, request.FILES, instance=video, user=request.user)
        if form.is_valid():
            # Save
            video = form.save(commit=False)
            video.file_size = video.file.size
            video.save()

            # Success! Send back an edit form
            for backend in get_search_backends():
                backend.add(video)

            messages.success(request, _("Video '{0}' added.").format(video.title), buttons=[
                messages.button(reverse('wagtailvideos:edit', args=(video.id,)), _('Edit'))
            ])
            return redirect('wagtailvideos:index')
        else:
            messages.error(request, _("The video could not be created due to errors."))
    else:
        form = VideoForm(user=request.user)

    return render(request, "wagtailvideos/videos/add.html", {
        'form': form,
    })


class UsageView(generic.UsageView):
    model = Video
    permission_policy = permission_policy
    pk_url_kwarg = "video_id"
    permission_required = "change"
    header_icon = "media"
    index_url_name = "wagtailvideos:index"
    edit_url_name = "wagtailvideos:edit"
    usage_url_name = "wagtailvideos:video_usage"

    def user_has_permission(self, permission):
        return self.permission_policy.user_has_permission_for_instance(
            self.request.user, permission, self.object
        )

    def get_page_subtitle(self):
        return self.object.title
