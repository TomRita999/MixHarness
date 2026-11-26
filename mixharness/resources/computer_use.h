#pragma once
#import <Carbon/Carbon.h>
#import <CommonCrypto/CommonDigest.h>

static NSArray<NSString *> *computerOperations(void) {
    return @[@"computer_list", @"computer_capture", @"computer_inspect", @"computer_focus",
             @"computer_click", @"computer_double_click", @"computer_drag", @"computer_type",
             @"computer_scroll", @"computer_key"];
}

static NSString *displayKey(SCDisplay *display) {
    return [NSString stringWithFormat:@"display:%u", display.displayID];
}

static BOOL permittedOwner(NSRunningApplication *app) {
    return app && !app.terminated && app.processIdentifier > 0 && app.processIdentifier != getppid() &&
           ![app.bundleIdentifier isEqualToString:@"local.mixharness.desktop"];
}

static NSString *applicationKey(NSRunningApplication *app) {
    if (!permittedOwner(app) || !app.launchDate) return nil;
    return [NSString stringWithFormat:@"%d-%lld", app.processIdentifier,
            llround(app.launchDate.timeIntervalSince1970 * 1000000)];
}

static void listComputer(SCShareableContent *content) {
    NSMutableArray *displays = [NSMutableArray array], *apps = [NSMutableArray array], *windows = [NSMutableArray array];
    for (SCDisplay *display in content.displays) {
        [displays addObject:@{@"target":displayKey(display), @"main":@(display.displayID == CGMainDisplayID()),
                              @"bounds":bounds(CGDisplayBounds(display.displayID))}];
    }
    for (NSRunningApplication *app in NSWorkspace.sharedWorkspace.runningApplications) {
        NSString *key = applicationKey(app);
        if (key && app.activationPolicy == NSApplicationActivationPolicyRegular) {
            [apps addObject:@{@"id":key, @"name":app.localizedName ?: @"",
                              @"bundle":app.bundleIdentifier ?: @"", @"active":@(app.active)}];
        }
    }
    for (SCWindow *window in content.windows) {
        NSString *key = windowKey(window);
        if (!key) continue;
        NSRunningApplication *owner = [NSRunningApplication runningApplicationWithProcessIdentifier:window.owningApplication.processID];
        NSString *app = applicationKey(owner);
        if (!app) continue;
        NSMutableDictionary *entry = [descriptionOf(window, key) mutableCopy];
        entry[@"application"] = app;
        entry[@"bounds"] = bounds(window.frame);
        [windows addObject:entry];
    }
    reply(@{@"displays":displays, @"applications":apps, @"windows":windows});
}

static NSString *displayState(SCDisplay *display) {
    if (!CGDisplayIsActive(display.displayID)) fail(@"显示器已断开，请重新读取桌面。");
    CGRect screen = CGDisplayBounds(display.displayID);
    NSMutableArray *scene = [NSMutableArray arrayWithObjects:bounds(screen),
        @(NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier), nil];
    NSArray *records = CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID));
    if (!records) fail(@"无法读取当前窗口排列。");
    for (NSDictionary *record in records) {
        CGRect rect;
        NSDictionary *position = record[(__bridge NSString *)kCGWindowBounds];
        if (!position || !CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)position, &rect) ||
            !CGRectIntersectsRect(rect, screen) || [record[(__bridge NSString *)kCGWindowAlpha] doubleValue] <= 0) continue;
        [scene addObject:@[record[(__bridge NSString *)kCGWindowNumber], record[(__bridge NSString *)kCGWindowOwnerPID],
                           record[(__bridge NSString *)kCGWindowLayer], bounds(rect)]];
    }
    NSData *data = [NSJSONSerialization dataWithJSONObject:scene options:0 error:NULL];
    if (!data) fail(@"无法记录显示器状态。");
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data.bytes, (CC_LONG)data.length, digest);
    return [[NSData dataWithBytes:digest length:sizeof(digest)] base64EncodedStringWithOptions:0];
}

static void captureDisplay(SCDisplay *display) API_AVAILABLE(macos(14.2));
static void captureDisplay(SCDisplay *display) {
    CGRect rect = CGDisplayBounds(display.displayID);
    if (CGRectIsEmpty(rect)) fail(@"显示器没有可读取的画面。");
    NSString *stamp = displayState(display);
    SCContentFilter *filter = [[SCContentFilter alloc] initWithDisplay:display excludingWindows:@[]];
    SCStreamConfiguration *config = [[SCStreamConfiguration alloc] init];
    double scale = fmin(1.0, 1600.0 / fmax(rect.size.width, rect.size.height));
    config.width = MAX(1, lround(rect.size.width * scale));
    config.height = MAX(1, lround(rect.size.height * scale));
    config.showsCursor = NO;
    [SCScreenshotManager captureImageWithFilter:filter configuration:config completionHandler:^(CGImageRef image, NSError *error) {
        if (!image || error) fail(error.localizedDescription ?: @"无法读取显示器画面。");
        if (![stamp isEqual:displayState(display)]) fail(@"截图期间窗口排列发生变化，请重新截图。");
        sendPicture(image, rect, stamp);
    }];
}

static CGRect accessibilityFrame(id element) {
    id position = attribute(element, kAXPositionAttribute), size = attribute(element, kAXSizeAttribute);
    CGPoint origin;
    CGSize extent;
    if (!position || !size || CFGetTypeID((__bridge CFTypeRef)position) != AXValueGetTypeID() ||
        CFGetTypeID((__bridge CFTypeRef)size) != AXValueGetTypeID() ||
        !AXValueGetValue((__bridge AXValueRef)position, kAXValueCGPointType, &origin) ||
        !AXValueGetValue((__bridge AXValueRef)size, kAXValueCGSizeType, &extent)) return CGRectNull;
    return (CGRect){origin, extent};
}

static BOOL secureElement(id element) {
    return [attribute(element, kAXSubroleAttribute) isEqual:(__bridge NSString *)kAXSecureTextFieldSubrole];
}

static void readControls(id element, NSUInteger depth, NSMutableArray *nodes, BOOL *truncated) {
    if (nodes.count >= 128) { *truncated = YES; return; }
    BOOL secure = secureElement(element);
    NSMutableDictionary *node = [@{@"depth":@(depth), @"role":attribute(element, kAXRoleAttribute) ?: @""} mutableCopy];
    [nodes addObject:node];
    if (secure) { node[@"protected"] = @YES; return; }
    NSArray *attributes = @[(__bridge NSString *)kAXTitleAttribute, (__bridge NSString *)kAXDescriptionAttribute,
                            (__bridge NSString *)kAXValueAttribute];
    for (NSString *name in attributes) {
        id value = attribute(element, (__bridge CFStringRef)name);
        if ([value isKindOfClass:NSString.class] && [value length]) {
            node[name] = [value substringToIndex:MIN((NSUInteger)180, [value length])];
        } else if ([value isKindOfClass:NSNumber.class]) node[name] = value;
    }
    CFIndex count = 0;
    AXUIElementRef ax = (__bridge AXUIElementRef)element;
    AXError error = AXUIElementGetAttributeValueCount(ax, kAXChildrenAttribute, &count);
    if (error == kAXErrorAttributeUnsupported || error == kAXErrorNoValue) return;
    if (error != kAXErrorSuccess) fail(@"应用未能返回辅助功能子控件。");
    if (count <= 0) return;
    if (depth >= 12 || nodes.count >= 128) { *truncated = YES; return; }
    CFIndex available = MIN(count, (CFIndex)(128 - nodes.count));
    if (available < count) *truncated = YES;
    CFArrayRef children = NULL;
    if (AXUIElementCopyAttributeValues(ax, kAXChildrenAttribute, 0, available, &children) != kAXErrorSuccess)
        fail(@"读取期间控件已变化，请重新读取。");
    for (id child in CFBridgingRelease(children)) {
        if (nodes.count >= 128) { *truncated = YES; break; }
        readControls(child, depth + 1, nodes, truncated);
    }
}

static CGPoint computerPoint(NSDictionary *request, CGRect rect, NSString *xName, NSString *yName) {
    NSNumber *x = request[xName], *y = request[yName];
    if (![x isKindOfClass:NSNumber.class] || ![y isKindOfClass:NSNumber.class] ||
        !(x.doubleValue >= 0 && x.doubleValue < 1 && y.doubleValue >= 0 && y.doubleValue < 1))
        fail(@"操作坐标超出了截图。");
    return CGPointMake(rect.origin.x + rect.size.width * x.doubleValue, rect.origin.y + rect.size.height * y.doubleValue);
}

static void checkPoint(CGPoint point, id application, id window) {
    id root = application ?: CFBridgingRelease(AXUIElementCreateSystemWide());
    AXUIElementRef hit = NULL;
    AXError error = AXUIElementCopyElementAtPosition((__bridge AXUIElementRef)root, point.x, point.y, &hit);
    id element = CFBridgingRelease(hit);
    pid_t pid = 0;
    if (error != kAXErrorSuccess || !element || AXUIElementGetPid((__bridge AXUIElementRef)element, &pid) != kAXErrorSuccess)
        fail(@"无法确认此处接收输入的应用。");
    if (!permittedOwner([NSRunningApplication runningApplicationWithProcessIdentifier:pid]))
        fail(@"此位置属于会话控制界面或不可操作的系统界面。");
    if (window && !belongs(element, window)) fail(@"该位置已不属于指定窗口，请重新截图。");
    if (secureElement(element)) fail(@"该控件是受保护的输入框。");
}

static void postPointer(CGEventRef event, SCWindow *window) {
    if (!event) fail(@"系统未能创建鼠标事件。");
    CGEventSetFlags(event, 0);
    if (window) {
        CGEventSetIntegerValueField(event, kCGMouseEventWindowUnderMousePointer, window.windowID);
        CGEventSetIntegerValueField(event, kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent, window.windowID);
        CGEventPostToPid(window.owningApplication.processID, event);
    } else CGEventPost(kCGHIDEventTap, event);
    CFRelease(event);
}

static void mouseAt(SCWindow *window, CGEventType type, CGPoint point, NSInteger clicks) {
    CGEventRef event = CGEventCreateMouseEvent(NULL, type, point, kCGMouseButtonLeft);
    if (!event) fail(@"系统未能创建鼠标事件。");
    CGEventSetIntegerValueField(event, kCGMouseEventClickState, clicks);
    postPointer(event, window);
}

static void sendPointer(NSDictionary *request, NSString *op, CGRect rect, SCWindow *window, id app, id axWindow) {
    CGPoint point = computerPoint(request, rect, @"x", @"y");
    checkPoint(point, app, axWindow);
    if ([op isEqual:@"computer_scroll"]) {
        NSNumber *x = request[@"delta_x"], *y = request[@"delta_y"];
        if (![x isKindOfClass:NSNumber.class] || ![y isKindOfClass:NSNumber.class] ||
            fabs(x.doubleValue) > 2000 || fabs(y.doubleValue) > 2000 || (!x.intValue && !y.intValue)) fail(@"滚动距离无效。");
        CGEventRef scroll = CGEventCreateScrollWheelEvent(NULL, kCGScrollEventUnitPixel, 2, -y.intValue, -x.intValue);
        if (!scroll) fail(@"系统未能创建滚动事件。");
        CGEventSetLocation(scroll, point);
        postPointer(scroll, window);
    } else if ([op isEqual:@"computer_drag"]) {
        CGPoint end = computerPoint(request, rect, @"to_x", @"to_y");
        checkPoint(end, app, axWindow);
        mouseAt(window, kCGEventLeftMouseDown, point, 1);
        for (int step = 1; step <= 12; step++) {
            CGPoint next = CGPointMake(point.x + (end.x - point.x) * step / 12, point.y + (end.y - point.y) * step / 12);
            usleep(10000);
            mouseAt(window, kCGEventLeftMouseDragged, next, 1);
        }
        mouseAt(window, kCGEventLeftMouseUp, end, 1);
    } else {
        int count = [op isEqual:@"computer_double_click"] ? 2 : 1;
        for (int n = 1; n <= count; n++) {
            mouseAt(window, kCGEventLeftMouseDown, point, n);
            mouseAt(window, kCGEventLeftMouseUp, point, n);
            if (n < count) usleep(35000);
        }
    }
}

static void sendKey(pid_t pid, CGKeyCode code, CGEventFlags flags, NSString *text) {
    unichar characters[20];
    if (text.length > 20) fail(@"单次键盘输入过长。");
    if (text) [text getCharacters:characters range:NSMakeRange(0, text.length)];
    for (int i = 0; i < 2; i++) {
        CGEventRef event = CGEventCreateKeyboardEvent(NULL, code, i == 0);
        if (!event) fail(@"系统未能创建键盘事件。");
        CGEventSetFlags(event, flags);
        if (text) CGEventKeyboardSetUnicodeString(event, text.length, characters);
        CGEventPostToPid(pid, event);
        CFRelease(event);
    }
}

static void sendShortcut(pid_t pid, NSString *chord) {
    if (![chord isKindOfClass:NSString.class] || !chord.length || chord.length > 64) fail(@"快捷键格式无效。");
    NSArray<NSString *> *keys = [[chord lowercaseString] componentsSeparatedByString:@"+"];
    NSDictionary *modifiers = @{@"cmd":@(kCGEventFlagMaskCommand), @"ctrl":@(kCGEventFlagMaskControl),
                                @"shift":@(kCGEventFlagMaskShift), @"option":@(kCGEventFlagMaskAlternate)};
    CGEventFlags flags = 0;
    for (NSUInteger i = 0; i + 1 < keys.count; i++) {
        NSNumber *flag = modifiers[keys[i]];
        if (!flag || (flags & flag.unsignedLongLongValue)) fail(@"快捷键修饰键无效。");
        flags |= flag.unsignedLongLongValue;
    }
    NSDictionary *special = @{@"return":@(kVK_Return),@"tab":@(kVK_Tab),@"space":@(kVK_Space),
        @"escape":@(kVK_Escape),@"backspace":@(kVK_Delete),@"delete":@(kVK_ForwardDelete),
        @"home":@(kVK_Home),@"end":@(kVK_End),@"pageup":@(kVK_PageUp),@"pagedown":@(kVK_PageDown),
        @"left":@(kVK_LeftArrow),@"right":@(kVK_RightArrow),@"up":@(kVK_UpArrow),@"down":@(kVK_DownArrow)};
    NSString *key = keys.lastObject;
    NSNumber *code = special[key];
    if (!code && key.length == 1) {
        NSString *letters = @"abcdefghijklmnopqrstuvwxyz0123456789";
        const CGKeyCode codes[] = {kVK_ANSI_A,kVK_ANSI_B,kVK_ANSI_C,kVK_ANSI_D,kVK_ANSI_E,kVK_ANSI_F,kVK_ANSI_G,
            kVK_ANSI_H,kVK_ANSI_I,kVK_ANSI_J,kVK_ANSI_K,kVK_ANSI_L,kVK_ANSI_M,kVK_ANSI_N,kVK_ANSI_O,kVK_ANSI_P,
            kVK_ANSI_Q,kVK_ANSI_R,kVK_ANSI_S,kVK_ANSI_T,kVK_ANSI_U,kVK_ANSI_V,kVK_ANSI_W,kVK_ANSI_X,kVK_ANSI_Y,
            kVK_ANSI_Z,kVK_ANSI_0,kVK_ANSI_1,kVK_ANSI_2,kVK_ANSI_3,kVK_ANSI_4,kVK_ANSI_5,kVK_ANSI_6,kVK_ANSI_7,
            kVK_ANSI_8,kVK_ANSI_9};
        NSRange location = [letters rangeOfString:key options:NSLiteralSearch];
        if (location.location != NSNotFound) code = @(codes[location.location]);
    }
    if (!code) fail(@"不支持此按键。");
    sendKey(pid, code.unsignedShortValue, flags, nil);
}

static void runComputer(NSDictionary *request, SCShareableContent *content) API_AVAILABLE(macos(14.2));
static void runComputer(NSDictionary *request, SCShareableContent *content) {
    NSString *op = request[@"op"], *target = request[@"target"];
    if ([op isEqual:@"computer_list"]) listComputer(content);
    SCDisplay *display = nil;
    SCWindow *window = nil;
    for (SCDisplay *candidate in content.displays) if ([displayKey(candidate) isEqual:target]) { display = candidate; break; }
    if (!display) {
        for (SCWindow *candidate in content.windows) if ([windowKey(candidate) isEqual:target]) { window = candidate; break; }
        if (!window) fail(@"显示器或目标窗口已关闭，请重新列出桌面和窗口。");
        checkLiveWindow(window, target);
    }
    if ([op isEqual:@"computer_capture"]) {
        if (display) captureDisplay(display);
        else capture(window, target);
        return;
    }
    if (!AXIsProcessTrusted()) fail(@"读取控件或操作桌面需要 macOS 辅助功能权限。");
    CGRect rect = display ? CGDisplayBounds(display.displayID) : window.frame;
    BOOL inspect = [op isEqual:@"computer_inspect"];
    if (!inspect && (![request[@"bounds"] isEqual:bounds(rect)] ||
        (display && ![request[@"stamp"] isEqual:displayState(display)]))) fail(@"画面或窗口排列已经变化，请重新截图。");
    BOOL pointed = [@[@"computer_click", @"computer_double_click", @"computer_scroll", @"computer_drag"] containsObject:op];
    NSRunningApplication *owner = window ? [NSRunningApplication runningApplicationWithProcessIdentifier:window.owningApplication.processID]
                                         : NSWorkspace.sharedWorkspace.frontmostApplication;
    id app = nil, axWindow = nil;
    if (window || !pointed) {
        if (!permittedOwner(owner)) fail(@"当前应用不能接收此操作。");
        app = CFBridgingRelease(AXUIElementCreateApplication(owner.processIdentifier));
        AXUIElementSetMessagingTimeout((__bridge AXUIElementRef)app, 1.25);
        axWindow = window ? accessibilityWindow(app, window, content.windows) : attribute(app, kAXFocusedWindowAttribute);
        if (!axWindow || (display && !CGRectIntersectsRect(accessibilityFrame(axWindow), rect)))
            fail(@"指定显示器上没有当前获得焦点的应用窗口。");
    }
    if (inspect) {
        NSMutableArray *nodes = [NSMutableArray array];
        BOOL truncated = NO;
        readControls(axWindow, 0, nodes, &truncated);
        reply(@{@"target":target, @"controls":nodes, @"truncated":@(truncated)});
    }
    if (window) checkLiveWindow(window, target);
    else if (![request[@"stamp"] isEqual:displayState(display)]) fail(@"桌面状态已变化，请重新截图。");
    if ([op isEqual:@"computer_focus"]) {
        if (!window) fail(@"切换焦点需要指定窗口。");
        if (!owner.active && ![owner activateWithOptions:NSApplicationActivateIgnoringOtherApps]) fail(@"系统未允许激活所选应用。");
        perform(axWindow, kAXRaiseAction);
    } else if (pointed) {
        sendPointer(request, op, rect, window, app, axWindow);
    } else {
        id focused = attribute(app, kAXFocusedUIElementAttribute);
        if (!belongs(focused, axWindow) || ![attribute(app, kAXFocusedWindowAttribute) isEqual:axWindow] || secureElement(focused))
            fail(@"输入焦点已变化或位于受保护的控件。");
        if ([op isEqual:@"computer_key"]) sendShortcut(owner.processIdentifier, request[@"key"]);
        else if ([op isEqual:@"computer_type"]) {
            NSString *text = request[@"text"];
            if (![text isKindOfClass:NSString.class] || !text.length || text.length > 8192) fail(@"输入文本长度无效。");
            for (NSUInteger index = 0; index < text.length;) {
                if (![attribute(app, kAXFocusedUIElementAttribute) isEqual:focused] ||
                    ![attribute(app, kAXFocusedWindowAttribute) isEqual:axWindow] ||
                    (display && NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier != owner.processIdentifier))
                    fail(@"输入期间焦点发生变化。");
                NSUInteger length = MIN((NSUInteger)20, text.length - index);
                if (index + length < text.length && CFStringIsSurrogateHighCharacter([text characterAtIndex:index + length - 1])) length--;
                sendKey(owner.processIdentifier, 0, 0, [text substringWithRange:NSMakeRange(index, length)]);
                index += length;
            }
        } else fail(@"未知桌面操作。");
    }
    reply(@{@"performed":op, @"target":target});
}
